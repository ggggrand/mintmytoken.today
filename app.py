#!/usr/bin/env python3
"""MintMyToken - Flask Backend"""

import os
import uuid
import logging
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename
from minting import build_mint_tx, merge_witness_and_submit
from vesting import (
    create_project, prepare_batches,
    build_batch_tx, submit_batch_tx,
    get_claimable, build_claim_tx,
    confirm_claim, submit_claim_tx,
    get_project_dashboard,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'change-me-in-production')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}
UPLOAD_FOLDER = Path('static/uploads')
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get('BASE_URL', 'https://mintmytoken.today')


def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/preprod')
def preprod():
    return render_template('preprod.html')

@app.route('/vest')
def vest():
    return render_template('vest.html')

@app.route('/claim')
def claim():
    return render_template('claim.html')

@app.route('/vest/dashboard')
def vest_dashboard():
    project_id = request.args.get('id', '')
    data = get_project_dashboard(project_id)
    if not data:
        return "Project not found", 404
    return render_template('vest_dashboard.html', data=data)


# ─── Static uploads ───────────────────────────────────────────────────────────

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ─── Minting API ──────────────────────────────────────────────────────────────

@app.route('/api/upload-image', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        return jsonify({'error': 'No image provided'}), 400
    file = request.files['image']
    if not file.filename or not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type'}), 400
    ext  = file.filename.rsplit('.', 1)[1].lower()
    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(UPLOAD_FOLDER / name)
    return jsonify({'url': f"{BASE_URL}/static/uploads/{name}"})


@app.route('/api/build-mint-tx', methods=['POST'])
def api_build_mint_tx():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    for field in ['token_name', 'token_amount', 'user_address', 'network']:
        if not data.get(field):
            return jsonify({'error': f'Missing field: {field}'}), 400

    token_name = data['token_name'].strip().upper()
    if not token_name.isalnum() or len(token_name) > 32:
        return jsonify({'error': 'Token name must be alphanumeric, max 32 chars'}), 400

    try:
        token_amount = int(data['token_amount'])
        assert 1 <= token_amount <= 1_000_000_000_000
    except Exception:
        return jsonify({'error': 'Invalid token amount'}), 400

    network = data.get('network', 'mainnet')
    if network not in ('preprod', 'mainnet'):
        return jsonify({'error': 'network must be preprod or mainnet'}), 400

    token_data = {
        'token_name':      token_name,
        'token_ticker':    data.get('token_ticker', '').strip().upper()[:8],
        'token_amount':    token_amount,
        'decimals':        max(0, min(int(data.get('decimals', 0)), 19)),
        'description':     data.get('description', '')[:255],
        'image_url':       data.get('image_url', ''),
        'receive_address': data.get('receive_address', '').strip(),
    }

    try:
        result = build_mint_tx(token_data, data['user_address'].strip(), network)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"build_mint_tx error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/submit-tx', methods=['POST'])
def api_submit_tx():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    wallet_witness_hex = data.get('wallet_witness_hex', '')
    server_tx_cbor     = data.get('server_tx_cbor', '')
    network            = data.get('network', 'mainnet')

    if not wallet_witness_hex:
        return jsonify({'error': 'wallet_witness_hex is required'}), 400
    if not server_tx_cbor:
        return jsonify({'error': 'server_tx_cbor is required'}), 400

    try:
        tx_hash = merge_witness_and_submit(server_tx_cbor, wallet_witness_hex, network)
        return jsonify({'tx_hash': tx_hash, 'network': network})
    except Exception as e:
        logging.error(f"submit error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ─── Vesting API ──────────────────────────────────────────────────────────────

@app.route('/api/create-vest-project', methods=['POST'])
def api_create_vest_project():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON required'}), 400
    try:
        project_id, all_tranches = create_project(data)
        batches = prepare_batches(project_id)
        return jsonify({
            'project_id':     project_id,
            'total_tranches': len(all_tranches),
            'batches':        batches,
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"create-vest-project: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/build-batch-tx', methods=['POST'])
def api_build_batch_tx():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON required'}), 400
    try:
        result = build_batch_tx(data['batch_id'], data['owner_address'])
        return jsonify(result)
    except Exception as e:
        logging.error(f"build-batch-tx: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/submit-batch-tx', methods=['POST'])
def api_submit_batch_tx():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON required'}), 400
    try:
        tx_hash = submit_batch_tx(data['batch_id'], data['wallet_witness_hex'])
        return jsonify({'tx_hash': tx_hash})
    except Exception as e:
        logging.error(f"submit-batch-tx: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/vest-project/<project_id>')
def api_vest_project(project_id):
    data = get_project_dashboard(project_id)
    if not data:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(data)


@app.route('/api/claim-tranches')
def api_claim_tranches():
    address = request.args.get('address', '').strip()
    network = request.args.get('network', 'mainnet')
    if not address:
        return jsonify({'error': 'address required'}), 400
    try:
        tranches = get_claimable(address, network)
        return jsonify({'tranches': tranches})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/build-claim-tx', methods=['POST'])
def api_build_claim_tx():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON required'}), 400
    try:
        result = build_claim_tx(data['tranche_id'], data['recipient_address'], data['network'])
        return jsonify(result)
    except Exception as e:
        logging.error(f"build-claim-tx: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/submit-claim-tx', methods=['POST'])
def api_submit_claim_tx():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON required'}), 400
    try:
        tx_hash = submit_claim_tx(
            data['server_tx_cbor'],
            data['wallet_witness_hex'],
            data['network']
        )
        confirm_claim(data['tranche_id'], tx_hash)
        return jsonify({'tx_hash': tx_hash})
    except Exception as e:
        logging.error(f"submit-claim-tx: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5003, debug=False)
