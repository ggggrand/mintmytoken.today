#!/usr/bin/env python3
"""MintMyToken - PyCardano Minting Engine"""

import os
import uuid
import logging
from pathlib import Path
import cbor2

from pycardano import (
    Network, BlockFrostChainContext,
    PaymentSigningKey, PaymentVerificationKey,
    Address, TransactionOutput, TransactionBuilder,
    Value, MultiAsset, ScriptPubkey,
    AuxiliaryData, Metadata,
    Transaction, TransactionWitnessSet, VerificationKeyWitness,
)

PREPROD_BLOCKFROST_ID   = os.environ.get('PREPROD_BLOCKFROST_ID', 'preprod1IkhWW2lLPSVgNRu6yxs7CRjRjMwOjaA')
MAINNET_BLOCKFROST_ID   = os.environ.get('MAINNET_BLOCKFROST_ID', '')
SERVICE_ADDRESS_PREPROD = os.environ.get('SERVICE_ADDRESS_PREPROD', '')
SERVICE_ADDRESS_MAINNET = os.environ.get('SERVICE_ADDRESS_MAINNET', '')
MINT_FEE_LOVELACE       = 50_000_000
TOKEN_MIN_UTXO          = 2_000_000

KEYS_DIR = Path('keys')
KEYS_DIR.mkdir(exist_ok=True)


def get_context(network_name):
    if network_name == 'preprod':
        return BlockFrostChainContext(project_id=PREPROD_BLOCKFROST_ID, network=Network.TESTNET)
    return BlockFrostChainContext(project_id=MAINNET_BLOCKFROST_ID, network=Network.MAINNET)


def to_hex(v):
    """to_cbor() returns bytes in some PyCardano versions, hex str in others."""
    return v.hex() if isinstance(v, bytes) else v


def split_64(s):
    """Split string into <=64-byte chunks for Cardano metadata."""
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


def get_service_address(network_name):
    addr = SERVICE_ADDRESS_PREPROD if network_name == 'preprod' else SERVICE_ADDRESS_MAINNET
    if not addr:
        raise ValueError(
            f"SERVICE_ADDRESS_{network_name.upper()} is not set in .env — "
            "add your wallet address so the 50 ADA fee goes to you!"
        )
    return addr


def build_mint_tx(token_data, user_address, network_name):
    """
    Build minting TX for the user's CIP-30 wallet to sign.
    Returns dict with tx_cbor (hex) ready for wallet.signTx().
    """
    context = get_context(network_name)

    # CIP-30 wallets return address as raw hex bytes — convert to bech32
    if not user_address.startswith('addr'):
        try:
            user_address = str(Address.from_primitive(bytes.fromhex(user_address)))
        except Exception as e:
            raise ValueError(f"Could not decode wallet address: {e}")

    try:
        utxos = context.utxos(user_address)
    except Exception as e:
        raise ValueError(f"Could not fetch UTXOs: {e}")

    if not utxos:
        raise ValueError("No UTXOs found. Make sure your wallet has ADA on the correct network.")

    total_ada = sum(u.output.amount.coin for u in utxos) / 1_000_000
    needed    = (MINT_FEE_LOVELACE + TOKEN_MIN_UTXO) / 1_000_000 + 1
    if total_ada < needed:
        raise ValueError(f"Insufficient balance. Need ~{needed:.0f} ADA, wallet has {total_ada:.2f} ADA.")

    # Ephemeral policy keypair
    session_id  = str(uuid.uuid4())
    policy_skey = PaymentSigningKey.generate()
    policy_vkey = PaymentVerificationKey.from_signing_key(policy_skey)
    (KEYS_DIR / f"{session_id}.skey").write_text(policy_skey.to_json())

    policy_script = ScriptPubkey(policy_vkey.hash())
    policy_id     = policy_script.hash()
    policy_id_hex = policy_id.payload.hex()

    token_name   = token_data['token_name']
    token_amount = token_data['token_amount']

    token_asset = MultiAsset.from_primitive({
        policy_id.payload: {token_name.encode(): token_amount}
    })

    cip25_meta = {
        721: {
            policy_id_hex: {
                token_name: {
                    "name":        token_data.get('token_ticker') or token_name,
                    "description": split_64(token_data.get('description', '')),
                    "image":       split_64(token_data.get('image_url', '')),
                    "ticker":      token_data.get('token_ticker', ''),
                    "decimals":    int(token_data.get('decimals', 0)),
                    "mediaType":   "image/png",
                }
            }
        }
    }

    user_addr    = Address.from_primitive(user_address)
    service_addr = Address.from_primitive(get_service_address(network_name))

    builder = TransactionBuilder(context)
    for utxo in utxos:
        builder.add_input(utxo)

    # Output 1: tokens to user
    builder.add_output(TransactionOutput(user_addr, Value(TOKEN_MIN_UTXO, token_asset)))
    # Output 2: 50 ADA service fee to YOUR wallet
    builder.add_output(TransactionOutput(service_addr, MINT_FEE_LOVELACE))

    builder.mint           = token_asset
    builder.native_scripts = [policy_script]
    builder.auxiliary_data = AuxiliaryData(Metadata(cip25_meta))

    # builder.build() returns TransactionBody directly
    tx_body = builder.build(change_address=user_addr)

    # Sign with policy key
    raw_sig = policy_skey.sign(bytes(tx_body.hash()))
    vk_wit  = VerificationKeyWitness(policy_vkey, raw_sig)

    witness_set = TransactionWitnessSet(
        vkey_witnesses=[vk_wit],
        native_scripts=[policy_script]
    )
    signed_tx = Transaction(tx_body, witness_set, auxiliary_data=builder.auxiliary_data)

    tx_cbor_hex = to_hex(signed_tx.to_cbor())
    logging.info(f"[build_mint_tx] policy={policy_id_hex} net={network_name}")
    return {
        'tx_cbor':      tx_cbor_hex,
        'policy_id':    policy_id_hex,
        'session_key':  session_id,
        'token_name':   token_name,
        'token_amount': token_amount,
        'network':      network_name,
    }


def merge_witness_and_submit(server_tx_cbor, wallet_witness_hex, network_name):
    """
    CIP-30 wallet.signTx() returns a TransactionWitnessSet CBOR hex (not a full TX).
    Merge it with the server TX (which has the policy witness) and submit.
    """
    # Parse the server TX — it has the policy vkey witness + native script
    server_tx = Transaction.from_cbor(server_tx_cbor)

    # Parse the wallet witness set CBOR
    ws_bytes = bytes.fromhex(wallet_witness_hex)
    ws_raw   = cbor2.loads(ws_bytes)

    # Witness set is a map: {0: [[vkey_bytes, sig_bytes], ...], ...}
    wallet_vkeys = []
    vkey_list = ws_raw.get(0, []) if isinstance(ws_raw, dict) else []
    for w in vkey_list:
        vk  = PaymentVerificationKey.from_primitive(w[0])
        sig = w[1]   # raw bytes — accepted directly by VerificationKeyWitness
        wallet_vkeys.append(VerificationKeyWitness(vk, sig))

    if not wallet_vkeys:
        raise ValueError("Wallet returned no vkey witnesses — did you cancel the signing?")

    # Merge: server policy witness + native scripts + wallet input witnesses
    existing = list(server_tx.transaction_witness_set.vkey_witnesses or [])
    merged_ws = TransactionWitnessSet(
        vkey_witnesses=existing + wallet_vkeys,
        native_scripts=server_tx.transaction_witness_set.native_scripts,
    )
    merged_tx = Transaction(
        server_tx.transaction_body,
        merged_ws,
        auxiliary_data=server_tx.auxiliary_data
    )

    context = get_context(network_name)
    context.submit_tx(merged_tx)
    tx_hash = str(merged_tx.id)
    logging.info(f"[submit] Success: {tx_hash}")
    return tx_hash
