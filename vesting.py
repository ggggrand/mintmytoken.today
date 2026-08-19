#!/usr/bin/env python3
import os, uuid, logging, sqlite3, csv, io
from datetime import datetime, timezone
from pathlib import Path
from pycardano import (
    Network, BlockFrostChainContext,
    PaymentSigningKey, PaymentVerificationKey,
    Address, TransactionOutput, TransactionBuilder,
    Value, MultiAsset, ScriptAll, ScriptPubkey, InvalidBefore,
    Transaction, TransactionWitnessSet, VerificationKeyWitness,
    AuxiliaryData, Metadata,
)
import cbor2

PREPROD_BLOCKFROST_ID   = os.environ.get('PREPROD_BLOCKFROST_ID', '')
MAINNET_BLOCKFROST_ID   = os.environ.get('MAINNET_BLOCKFROST_ID', '')
SERVICE_ADDRESS_PREPROD = os.environ.get('SERVICE_ADDRESS_PREPROD', '')
SERVICE_ADDRESS_MAINNET = os.environ.get('SERVICE_ADDRESS_MAINNET', '')
VEST_FEE_LOVELACE = 10_000_000   # 10 ADA service fee
MIN_UTXO          = 1_500_000    # 1.5 ADA min per script UTXO
OUTPUTS_PER_TX    = 20
DB_PATH  = Path('vesting.db')
KEYS_DIR = Path('keys')
KEYS_DIR.mkdir(exist_ok=True)
SLOT_OFFSET = {'mainnet': 1596491091 - 4492800, 'preprod': 1655769600}


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vest_projects (
                id TEXT PRIMARY KEY, network TEXT NOT NULL,
                owner_address TEXT NOT NULL, policy_id TEXT NOT NULL,
                asset_name TEXT NOT NULL, total_supply INTEGER NOT NULL,
                project_name TEXT, created_at INTEGER NOT NULL,
                status TEXT DEFAULT 'pending', service_tx_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS vest_groups (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                name TEXT NOT NULL, pct REAL NOT NULL,
                total_tokens INTEGER NOT NULL, vest_type TEXT NOT NULL,
                cliff_unix INTEGER, end_unix INTEGER,
                num_tranches INTEGER DEFAULT 1,
                FOREIGN KEY (project_id) REFERENCES vest_projects(id)
            );
            CREATE TABLE IF NOT EXISTS vest_recipients (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                group_id TEXT NOT NULL, address TEXT NOT NULL,
                token_amount INTEGER NOT NULL, status TEXT DEFAULT 'pending',
                FOREIGN KEY (project_id) REFERENCES vest_projects(id),
                FOREIGN KEY (group_id) REFERENCES vest_groups(id)
            );
            CREATE TABLE IF NOT EXISTS vest_tranches (
                id TEXT PRIMARY KEY, recipient_id TEXT NOT NULL,
                project_id TEXT NOT NULL, tranche_index INTEGER NOT NULL,
                amount INTEGER NOT NULL, unlock_slot INTEGER NOT NULL,
                unlock_date TEXT NOT NULL, script_address TEXT NOT NULL,
                script_cbor TEXT, batch_id TEXT,
                utxo_tx_hash TEXT, utxo_tx_index INTEGER,
                status TEXT DEFAULT 'locked', claim_tx_hash TEXT,
                FOREIGN KEY (recipient_id) REFERENCES vest_recipients(id)
            );
            CREATE TABLE IF NOT EXISTS vest_batches (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL, tx_cbor TEXT, tx_hash TEXT,
                output_count INTEGER NOT NULL, status TEXT DEFAULT 'pending',
                FOREIGN KEY (project_id) REFERENCES vest_projects(id)
            );
        """)
        conn.commit()


def migrate_db():
    with sqlite3.connect(DB_PATH) as conn:
        for col in ['script_cbor']:
            try:
                conn.execute(f'ALTER TABLE vest_tranches ADD COLUMN {col} TEXT')
            except Exception:
                pass


init_db()
migrate_db()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_context(net):
    if net == 'preprod':
        return BlockFrostChainContext(project_id=PREPROD_BLOCKFROST_ID, network=Network.TESTNET)
    return BlockFrostChainContext(project_id=MAINNET_BLOCKFROST_ID, network=Network.MAINNET)


def to_hex(v):
    return v.hex() if isinstance(v, bytes) else v


def split_64(s):
    if not s:
        return ''
    data = s.encode('utf-8')
    if len(data) <= 64:
        return s
    chunks = []
    while data:
        chunks.append(data[:64].decode('utf-8', errors='replace'))
        data = data[64:]
    return chunks


def unix_to_slot(ts, net):
    return max(0, int(ts) - SLOT_OFFSET.get(net, SLOT_OFFSET['mainnet']))


def bech32addr(raw, net):
    if not raw or str(raw).startswith('addr'):
        return str(raw)
    try:
        import cbor2 as _c
        return str(Address.from_primitive(_c.loads(bytes.fromhex(raw))))
    except Exception:
        pass
    try:
        return str(Address.from_primitive(bytes.fromhex(raw)))
    except Exception:
        return str(raw)


def get_service_addr(net):
    a = SERVICE_ADDRESS_PREPROD if net == 'preprod' else SERVICE_ADDRESS_MAINNET
    if not a:
        raise ValueError(f"SERVICE_ADDRESS_{net.upper()} not set in .env")
    return a


def make_vest_script(recipient_addr_str, unlock_slot, net):
    addr = Address.from_primitive(recipient_addr_str)
    return ScriptAll([ScriptPubkey(addr.payment_part), InvalidBefore(unlock_slot)])


def script_address(script, net):
    network = Network.TESTNET if net == 'preprod' else Network.MAINNET
    return str(Address(payment_part=script.hash(), network=network))


def _fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# ─── CSV Parser ───────────────────────────────────────────────────────────────

def parse_csv_recipients(csv_content, group_total_tokens, network):
    reader = csv.DictReader(io.StringIO(csv_content.strip()))
    rows   = list(reader)
    if not rows:
        raise ValueError("CSV is empty")
    keys = list(rows[0].keys())
    if len(keys) < 2:
        raise ValueError("CSV must have at least 2 columns: address, amount")

    result   = []
    pct_mode = False
    total_pct = 0.0

    for i, row in enumerate(rows, 1):
        addr  = row.get('address', row.get(keys[0], '')).strip()
        raw_v = row.get('amount', row.get('percentage', row.get(keys[1], ''))).strip()
        if not addr or not addr.startswith('addr'):
            raise ValueError(f"Row {i}: invalid address")
        if not raw_v:
            raise ValueError(f"Row {i}: missing amount")
        if '%' in raw_v:
            pct_mode = True
            pct = float(raw_v.replace('%', '').strip())
            total_pct += pct
            result.append({'address': addr, '_pct': pct})
        else:
            result.append({'address': addr, 'token_amount': int(float(raw_v))})

    if pct_mode:
        if abs(total_pct - 100.0) > 0.01:
            raise ValueError(f"Percentages must sum to 100%. Got {total_pct:.2f}%")
        remainder = group_total_tokens
        for j, r in enumerate(result):
            amt = round(group_total_tokens * r['_pct'] / 100)
            if j == len(result) - 1:
                amt = remainder
            else:
                remainder -= amt
            r['token_amount'] = amt
        for r in result:
            r.pop('_pct', None)
    return result


# ─── Tranche Calculator ───────────────────────────────────────────────────────

def _calc_tranches(total, vest_type, cliff_unix, end_unix, num_tranches):
    if vest_type == 'cliff':
        ts = end_unix or cliff_unix
        return [(total, ts, _fmt(ts))]
    start = cliff_unix or end_unix
    end   = end_unix or cliff_unix
    span  = end - start
    base  = total // num_tranches
    rem   = total - base * num_tranches
    tranches = []
    for i in range(num_tranches):
        ts  = int(start + span * (i + 1) / num_tranches)
        amt = base + (rem if i == num_tranches - 1 else 0)
        tranches.append((amt, ts, _fmt(ts)))
    return tranches


# ─── Project Creation ─────────────────────────────────────────────────────────

def create_project(params):
    project_id   = str(uuid.uuid4())
    network      = params['network']
    total_supply = int(params['total_supply'])

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO vest_projects (id,network,owner_address,policy_id,asset_name,total_supply,project_name,created_at,status) VALUES (?,?,?,?,?,?,?,?,?)',
            (project_id, network, params.get('owner_address', ''), params['policy_id'],
             params['asset_name'], total_supply, params.get('project_name', ''),
             int(datetime.now().timestamp()), 'pending')
        )

        all_tranches = []
        for g in params['groups']:
            group_id     = str(uuid.uuid4())
            group_tokens = round(total_supply * float(g['pct']) / 100)

            conn.execute(
                'INSERT INTO vest_groups (id,project_id,name,pct,total_tokens,vest_type,cliff_unix,end_unix,num_tranches) VALUES (?,?,?,?,?,?,?,?,?)',
                (group_id, project_id, g['name'], float(g['pct']), group_tokens,
                 g['vest_type'], g.get('cliff_unix'), g.get('end_unix'), int(g.get('num_tranches', 1)))
            )

            recipients = parse_csv_recipients(g['recipients_csv'], group_tokens, network)
            for rec in recipients:
                rec_id = str(uuid.uuid4())
                addr   = rec['address']
                rec_amt = rec['token_amount']

                conn.execute(
                    'INSERT INTO vest_recipients (id,project_id,group_id,address,token_amount,status) VALUES (?,?,?,?,?,?)',
                    (rec_id, project_id, group_id, addr, rec_amt, 'pending')
                )

                tranches = _calc_tranches(
                    total=rec_amt, vest_type=g['vest_type'],
                    cliff_unix=g.get('cliff_unix'), end_unix=g.get('end_unix'),
                    num_tranches=int(g.get('num_tranches', 1))
                )

                for tidx, (tamount, tunix, tdate) in enumerate(tranches):
                    tid         = str(uuid.uuid4())
                    unlock_slot = unix_to_slot(tunix, network)
                    script      = make_vest_script(addr, unlock_slot, network)
                    sc_addr     = script_address(script, network)
                    script_cbor = to_hex(script.to_cbor())

                    conn.execute(
                        'INSERT INTO vest_tranches (id,recipient_id,project_id,tranche_index,amount,unlock_slot,unlock_date,script_address,script_cbor,status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (tid, rec_id, project_id, tidx, tamount, unlock_slot, tdate, sc_addr, script_cbor, 'locked')
                    )
                    all_tranches.append({'id': tid, 'script_address': sc_addr, 'amount': tamount})

        conn.commit()

    logging.info(f"[vest] Project {project_id}: {len(all_tranches)} tranches")
    return project_id, all_tranches


# ─── Batch Preparation ────────────────────────────────────────────────────────

def prepare_batches(project_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM vest_batches WHERE project_id=? AND status='pending'", (project_id,))
        tranches = conn.execute(
            'SELECT id, script_address, amount, script_cbor FROM vest_tranches WHERE project_id=? AND status=? AND utxo_tx_hash IS NULL ORDER BY rowid',
            (project_id, 'locked')
        ).fetchall()
        project = conn.execute(
            'SELECT policy_id, asset_name, network FROM vest_projects WHERE id=?', (project_id,)
        ).fetchone()

        batches = []
        chunks  = [tranches[i:i+OUTPUTS_PER_TX] for i in range(0, len(tranches), OUTPUTS_PER_TX)]
        for idx, chunk in enumerate(chunks):
            bid = str(uuid.uuid4())
            conn.execute(
                'INSERT INTO vest_batches (id,project_id,batch_index,output_count,status) VALUES (?,?,?,?,?)',
                (bid, project_id, idx, len(chunk), 'pending')
            )
            for (tid, _, _, _) in chunk:
                conn.execute('UPDATE vest_tranches SET batch_id=? WHERE id=?', (bid, tid))
            batches.append({
                'batch_id':    bid,
                'batch_index': idx,
                'total':       len(chunks),
                'output_count': len(chunk),
                'outputs':     [{'tranche_id': t[0], 'script_address': t[1], 'amount': t[2]} for t in chunk],
                'policy_id':   project[0],
                'asset_name':  project[1],
                'network':     project[2],
            })
        conn.commit()

    logging.info(f"[vest] {len(batches)} batches prepared for {project_id}")
    return batches


# ─── Build Batch TX ───────────────────────────────────────────────────────────

def build_batch_tx(batch_id, owner_address):
    with sqlite3.connect(DB_PATH) as conn:
        batch = conn.execute(
            'SELECT project_id, batch_index, status FROM vest_batches WHERE id=?', (batch_id,)
        ).fetchone()
        if not batch or batch[2] != 'pending':
            raise ValueError("Batch not found or already processed.")
        project_id = batch[0]
        policy_id, asset_name, network = conn.execute(
            'SELECT policy_id, asset_name, network FROM vest_projects WHERE id=?', (project_id,)
        ).fetchone()
        tranches = conn.execute(
            'SELECT id, script_address, amount, script_cbor FROM vest_tranches WHERE batch_id=? ORDER BY tranche_index',
            (batch_id,)
        ).fetchall()

    context = get_context(network)
    owner_address = bech32addr(owner_address, network)
    utxos = context.utxos(owner_address)
    if not utxos:
        raise ValueError("No UTXOs found in wallet.")

    policy_bytes     = bytes.fromhex(policy_id)
    asset_bytes      = asset_name.encode()
    owner_addr_obj   = Address.from_primitive(owner_address)
    builder          = TransactionBuilder(context)
    for u in utxos:
        builder.add_input(u)

    onchain_scripts = {}
    for (tid, sc_addr, amount, script_cbor) in tranches:
        token_asset = MultiAsset.from_primitive({policy_bytes: {asset_bytes: amount}})
        builder.add_output(TransactionOutput(Address.from_primitive(sc_addr), Value(MIN_UTXO, token_asset)))
        if script_cbor:
            key = sc_addr[:40]
            onchain_scripts[key] = {'s': split_64(script_cbor), 'a': split_64(sc_addr)}

    # Service fee on first batch only
    if batch[1] == 0:
        builder.add_output(TransactionOutput(Address.from_primitive(get_service_addr(network)), VEST_FEE_LOVELACE))

    # Embed scripts in TX metadata (website-independent claiming)
    if onchain_scripts:
        vest_meta = {674: {'v': 1, 't': 'mmtvest', 'sc': dict(list(onchain_scripts.items())[:10])}}
        builder.auxiliary_data = AuxiliaryData(Metadata(vest_meta))

    tx_body     = builder.build(change_address=owner_addr_obj)
    tx_cbor_hex = to_hex(Transaction(tx_body, TransactionWitnessSet()).to_cbor())

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('UPDATE vest_batches SET tx_cbor=? WHERE id=?', (tx_cbor_hex, batch_id))
        conn.commit()

    logging.info(f"[vest] Built batch {batch_id} ({len(tranches)} outputs)")
    return {'tx_cbor': tx_cbor_hex, 'batch_id': batch_id, 'output_count': len(tranches)}


# ─── Submit Batch TX ──────────────────────────────────────────────────────────

def submit_batch_tx(batch_id, wallet_witness_hex):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT tx_cbor, project_id, batch_index FROM vest_batches WHERE id=?', (batch_id,)
        ).fetchone()
        if not row:
            raise ValueError("Batch not found.")
        tx_cbor, project_id, batch_idx = row
        network = conn.execute(
            'SELECT network FROM vest_projects WHERE id=?', (project_id,)
        ).fetchone()[0]

    server_tx = Transaction.from_cbor(tx_cbor)
    ws_raw    = cbor2.loads(bytes.fromhex(wallet_witness_hex))
    wallet_vkeys = []
    for w in (ws_raw.get(0, []) if isinstance(ws_raw, dict) else []):
        wallet_vkeys.append(VerificationKeyWitness(PaymentVerificationKey.from_primitive(w[0]), w[1]))

    existing  = list(server_tx.transaction_witness_set.vkey_witnesses or [])
    merged_ws = TransactionWitnessSet(vkey_witnesses=existing + wallet_vkeys)
    merged_tx = Transaction(server_tx.transaction_body, merged_ws, auxiliary_data=server_tx.auxiliary_data)
    get_context(network).submit_tx(merged_tx)
    tx_hash = str(merged_tx.id)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('UPDATE vest_batches SET tx_hash=?, status=? WHERE id=?', (tx_hash, 'confirmed', batch_id))
        tranches = conn.execute(
            'SELECT id, tranche_index FROM vest_tranches WHERE batch_id=? ORDER BY tranche_index', (batch_id,)
        ).fetchall()
        for (tid, tidx) in tranches:
            conn.execute('UPDATE vest_tranches SET utxo_tx_hash=?, utxo_tx_index=? WHERE id=?', (tx_hash, tidx, tid))
        pending = conn.execute(
            "SELECT COUNT(*) FROM vest_batches WHERE project_id=? AND status='pending'", (project_id,)
        ).fetchone()[0]
        if pending == 0:
            conn.execute("UPDATE vest_projects SET status='active' WHERE id=?", (project_id,))
        conn.commit()

    logging.info(f"[vest] Batch {batch_id} confirmed: {tx_hash}")
    return tx_hash


# ─── Claim ────────────────────────────────────────────────────────────────────

def get_claimable(recipient_address, network):
    recipient_address = bech32addr(recipient_address, network)
    now_slot = unix_to_slot(datetime.now(timezone.utc).timestamp(), network)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('''
            SELECT t.id, t.amount, t.unlock_slot, t.unlock_date,
                   t.script_address, t.utxo_tx_hash, t.utxo_tx_index,
                   t.status, t.claim_tx_hash,
                   p.policy_id, p.asset_name, p.project_name, p.network,
                   g.name
            FROM vest_tranches t
            JOIN vest_recipients r ON t.recipient_id = r.id
            JOIN vest_projects p ON t.project_id = p.id
            JOIN vest_groups g ON r.group_id = g.id
            WHERE r.address=? AND p.network=?
            ORDER BY t.unlock_slot ASC
        ''', (recipient_address, network)).fetchall()

    cols = ['id','amount','unlock_slot','unlock_date','script_address',
            'utxo_tx_hash','utxo_tx_index','status','claim_tx_hash',
            'policy_id','asset_name','project_name','network','group_name']
    result = []
    for row in rows:
        d = dict(zip(cols, row))
        d['unlocked']  = now_slot >= d['unlock_slot'] and d['utxo_tx_hash'] is not None
        d['claimable'] = d['unlocked'] and d['status'] == 'locked'
        result.append(d)
    return result


def build_claim_tx(tranche_id, recipient_address, network):
    recipient_address = bech32addr(recipient_address, network)
    context = get_context(network)

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('''
            SELECT t.amount, t.unlock_slot, t.script_address,
                   t.utxo_tx_hash, t.utxo_tx_index, t.status,
                   t.script_cbor, p.policy_id, p.asset_name, r.address
            FROM vest_tranches t
            JOIN vest_recipients r ON t.recipient_id = r.id
            JOIN vest_projects p ON t.project_id = p.id
            WHERE t.id=?
        ''', (tranche_id,)).fetchone()

    if not row:
        raise ValueError("Tranche not found.")
    amount, unlock_slot, sc_addr, utxo_hash, utxo_idx, status, script_cbor, policy_id, asset_name, db_addr = row

    if bech32addr(db_addr, network) != recipient_address:
        raise ValueError("Address does not match vesting record.")
    if status == 'claimed':
        raise ValueError("Already claimed.")
    if not utxo_hash:
        raise ValueError("Vesting TX not yet confirmed. Please wait a few minutes.")

    now_slot = unix_to_slot(datetime.now(timezone.utc).timestamp(), network)
    if now_slot < unlock_slot:
        raise ValueError(f"Not unlocked yet. Unlocks at slot {unlock_slot} (now: {now_slot}).")

    # Rebuild script from stored CBOR — works without website
    try:
        from pycardano import NativeScript
        vest_script = NativeScript.from_cbor(script_cbor) if script_cbor else make_vest_script(recipient_address, unlock_slot, network)
    except Exception:
        vest_script = make_vest_script(recipient_address, unlock_slot, network)

    script_utxos = context.utxos(sc_addr)
    target = next((u for u in script_utxos
                   if str(u.input.transaction_id) == utxo_hash and u.input.index == utxo_idx), None)
    if not target:
        raise ValueError("Script UTXO not found. May already be claimed.")

    recip_utxos = context.utxos(recipient_address)
    if not recip_utxos:
        raise ValueError("No UTXOs in recipient wallet for fees.")

    recip_addr = Address.from_primitive(recipient_address)
    builder    = TransactionBuilder(context)
    builder.add_input(target)
    builder.native_scripts = [vest_script]
    builder.validity_start  = unlock_slot
    for u in recip_utxos[:3]:
        builder.add_input(u)

    token_asset = MultiAsset.from_primitive({bytes.fromhex(policy_id): {asset_name.encode(): amount}})
    builder.add_output(TransactionOutput(recip_addr, Value(MIN_UTXO, token_asset)))
    tx_body     = builder.build(change_address=recip_addr)
    tx_cbor_hex = to_hex(Transaction(tx_body, TransactionWitnessSet(native_scripts=[vest_script])).to_cbor())

    return {'tx_cbor': tx_cbor_hex, 'tranche_id': tranche_id,
            'amount': amount, 'policy_id': policy_id, 'asset_name': asset_name, 'network': network}


def confirm_claim(tranche_id, tx_hash):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('UPDATE vest_tranches SET status=?, claim_tx_hash=? WHERE id=?',
                     ('claimed', tx_hash, tranche_id))
        conn.commit()


def submit_claim_tx(server_tx_cbor, wallet_witness_hex, network):
    server_tx = Transaction.from_cbor(server_tx_cbor)
    ws_raw    = cbor2.loads(bytes.fromhex(wallet_witness_hex))
    wallet_vkeys = []
    for w in (ws_raw.get(0, []) if isinstance(ws_raw, dict) else []):
        wallet_vkeys.append(VerificationKeyWitness(PaymentVerificationKey.from_primitive(w[0]), w[1]))
    existing  = list(server_tx.transaction_witness_set.vkey_witnesses or [])
    merged_ws = TransactionWitnessSet(
        vkey_witnesses=existing + wallet_vkeys,
        native_scripts=server_tx.transaction_witness_set.native_scripts)
    merged_tx = Transaction(server_tx.transaction_body, merged_ws, auxiliary_data=server_tx.auxiliary_data)
    get_context(network).submit_tx(merged_tx)
    return str(merged_tx.id)


# ─── Dashboard ────────────────────────────────────────────────────────────────

def get_project_dashboard(project_id):
    with sqlite3.connect(DB_PATH) as conn:
        project = conn.execute('SELECT * FROM vest_projects WHERE id=?', (project_id,)).fetchone()
        if not project:
            return None
        groups  = conn.execute('SELECT * FROM vest_groups WHERE project_id=?', (project_id,)).fetchall()
        batches = conn.execute(
            'SELECT id,batch_index,output_count,status,tx_hash FROM vest_batches WHERE project_id=? ORDER BY batch_index',
            (project_id,)
        ).fetchall()
        stats = conn.execute('''
            SELECT COUNT(*),
                   SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='locked' AND utxo_tx_hash IS NOT NULL THEN 1 ELSE 0 END)
            FROM vest_tranches WHERE project_id=?
        ''', (project_id,)).fetchone()

    pcols = ['id','network','owner_address','policy_id','asset_name','total_supply',
             'project_name','created_at','status','service_tx_hash']
    gcols = ['id','project_id','name','pct','total_tokens','vest_type','cliff_unix','end_unix','num_tranches']
    bcols = ['id','batch_index','output_count','status','tx_hash']
    return {
        'project': dict(zip(pcols, project)),
        'groups':  [dict(zip(gcols, g)) for g in groups],
        'batches': [dict(zip(bcols, b)) for b in batches],
        'stats':   {'total_tranches': stats[0], 'claimed': stats[1] or 0, 'locked': stats[2] or 0},
    }
