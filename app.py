from flask import Flask, jsonify, render_template, request, send_file
from flask_sqlalchemy import SQLAlchemy
import tinytuya
import threading
import time
from datetime import datetime
from sqlalchemy import text
import pandas as pd
import io

app = Flask(__name__)

# ---- Configure your devices here ----
DEVICES = [
    {
        "name": "LivingRoom",
        "device_id": "bf5b8d9c7f3f2daa3f09du",
        "local_key": "B@94pU=Yh7^p5lT5",
        "ip": "192.168.0.106",
        "protocol": 3.5
    },
    {
        "name": "Bedroom",
        "device_id": "bf5b8d9c7f3f2daa3f09du",
        "local_key": "B@94pU=Yh7^p5lT5",
        "ip": "192.168.0.106",
        "protocol": 3.5
    }
]
# -------------------------------------

# SQLite Config
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///energy_data.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Model
class EnergyData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(50))  # store device name
    timestamp = db.Column(db.String(20))
    watt = db.Column(db.Float)
    voltage = db.Column(db.Float)   # stored as scaled value (V)
    current = db.Column(db.Float)   # as returned by device (mA or A depending on device)
    kwh = db.Column(db.Float)

with app.app_context():
    db.create_all()

# Polling Function
def poll_device(device_info, interval=10):
    """
    Polls a single device forever (daemon thread).
    Stores scaled voltage in DB (voltage / 10).
    """
    try:
        dev = tinytuya.OutletDevice(device_info["device_id"], device_info["ip"], device_info["local_key"])
        dev.set_version(float(device_info.get("protocol", 3.3)))
    except Exception as e:
        print(f"[{device_info['name']}] Error creating device object: {e}")
        return

    while True:
        try:
            data = dev.status()
            dp = data.get("dps", {})

            # dps keys from your device (confirm these are correct per model)
            raw_watt = dp.get("19", 0)      # example: needs device-specific check
            raw_voltage = dp.get("20", 0)
            raw_current = dp.get("18", 0)

            # Normalize/scale values as needed
            watt = raw_watt / 10  # if raw_watt is 10x
            voltage = raw_voltage / 10  # scale voltage once and store scaled
            current = raw_current       # keep as-is (mA) — change if device returns A

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            kwh = watt * (interval / 3600) / 1000  # W * hours = Wh -> /1000 -> kWh

            with app.app_context():
                entry = EnergyData(
                    device_id=device_info["name"],
                    timestamp=timestamp,
                    watt=watt,
                    voltage=voltage,
                    current=current,
                    kwh=kwh
                )
                db.session.add(entry)
                db.session.commit()

            print(f"[{device_info['name']}] {timestamp} → W: {watt} W, V: {voltage} V, A: {current}, kWh: {kwh:.6f}")

        except Exception as e:
            print(f"Error fetching data from {device_info['name']}: {e}")

        time.sleep(interval)


# Flag to avoid starting threads multiple times in dev reloads
polling_started = False

# Routes
@app.route('/')
def dashboard():
    global polling_started
    # Start polling threads once when dashboard is first requested
    if not polling_started:
        for device_info in DEVICES:
            t = threading.Thread(target=poll_device, args=(device_info,), daemon=True)
            t.start()
        polling_started = True
    return render_template('dashboard.html')


@app.route('/api/devices')
def api_devices():
    """Return list of device names for front-end selector."""
    names = [d['name'] for d in DEVICES]
    return jsonify(names)


@app.route('/api/data/<device_name>')
def get_data(device_name):
    """Return latest 60 rows for a device (oldest -> newest)."""
    entries = EnergyData.query.filter_by(device_id=device_name) \
                .order_by(EnergyData.id.desc()).limit(60).all()
    entries.reverse()
    return jsonify([{
        "timestamp": e.timestamp,
        "watt": e.watt,
        "voltage": e.voltage,
        "current": e.current
    } for e in entries])


@app.route('/api/total-kwh')
def total_kwh_all():
    """Total kWh across all devices."""
    total = db.session.query(db.func.sum(EnergyData.kwh)).scalar() or 0
    return jsonify({"total_kwh": round(total, 6)})


@app.route('/api/total-kwh/<device_name>')
def total_kwh_device(device_name):
    """Total kWh for a single device."""
    total = db.session.query(db.func.sum(EnergyData.kwh)).filter(EnergyData.device_id == device_name).scalar() or 0
    return jsonify({"total_kwh": round(total, 6)})


@app.route('/api/stats')
def energy_stats():
    """Daily and hourly aggregated kWh across all devices."""
    daily = db.session.execute(text("""
        SELECT SUBSTR(timestamp, 1, 10) AS day, SUM(kwh)
        FROM energy_data
        GROUP BY day
        ORDER BY day DESC
        LIMIT 7
    """)).fetchall()

    hourly = db.session.execute(text("""
        SELECT SUBSTR(timestamp, 1, 13) AS hour, SUM(kwh)
        FROM energy_data
        GROUP BY hour
        ORDER BY hour DESC
        LIMIT 24
    """)).fetchall()

    return jsonify({
        "daily": [{"day": d[0], "kwh": round(d[1] or 0, 6)} for d in daily],
        "hourly": [{"hour": h[0], "kwh": round(h[1] or 0, 6)} for h in hourly]
    })


@app.route('/api/stats/minutely')
def minutely_stats_all():
    """Minutely aggregation across all devices (last 60 minutes)."""
    results = db.session.execute(text("""
        SELECT SUBSTR(timestamp, 1, 16) AS minute, SUM(kwh) AS total_kwh
        FROM energy_data
        GROUP BY minute
        ORDER BY minute DESC
        LIMIT 60
    """)).fetchall()

    results = list(reversed(results))
    return jsonify([{"minute": r[0], "total_kwh": round(r[1] or 0, 6)} for r in results])


@app.route('/api/stats/minutely/<device_name>')
def minutely_stats_device(device_name):
    """Minutely aggregation for a single device."""
    results = db.session.execute(text("""
        SELECT SUBSTR(timestamp, 1, 16) AS minute, SUM(kwh) AS total_kwh
        FROM energy_data
        WHERE device_id = :dev
        GROUP BY minute
        ORDER BY minute DESC
        LIMIT 60
    """), {"dev": device_name}).fetchall()

    results = list(reversed(results))
    return jsonify([{"minute": r[0], "total_kwh": round(r[1] or 0, 6)} for r in results])


@app.route('/export/full-energy-report')
def export_full_energy_report():
    """
    Export full energy report to Excel. Optional query param ?device=DeviceName
    If device is omitted, export data for all devices.
    """
    device_name = request.args.get('device', None)

    if device_name:
        raw_data = db.session.execute(text("""
            SELECT timestamp, watt, current, voltage, kwh
            FROM energy_data
            WHERE device_id = :dev
            ORDER BY timestamp ASC
        """), {"dev": device_name}).fetchall()
    else:
        raw_data = db.session.execute(text("""
            SELECT timestamp, watt, current, voltage, kwh
            FROM energy_data
            ORDER BY timestamp ASC
        """)).fetchall()

    # raw_data rows are tuples: (timestamp, watt, current, voltage, kwh)
    # Build DataFrame and round values
    scaled_data = []
    for row in raw_data:
        timestamp, watt, current, voltage, kwh = row
        # voltage already scaled when saved to DB
        scaled_data.append((timestamp, watt, current, voltage, kwh))

    df_raw = pd.DataFrame(scaled_data, columns=['Timestamp', 'Watt', 'Current', 'Voltage', 'kWh'])
    if not df_raw.empty:
        df_raw['kWh'] = df_raw['kWh'].round(6)

    # Group by minute for minutely totals
    if not df_raw.empty:
        df_raw['Minute'] = df_raw['Timestamp'].astype(str).str.slice(0, 16)
        df_minutely = df_raw.groupby('Minute', as_index=False)['kWh'].sum()
        df_minutely.rename(columns={'kWh': 'Total_kWh'}, inplace=True)
        df_minutely['Total_kWh'] = df_minutely['Total_kWh'].round(6)
    else:
        df_minutely = pd.DataFrame(columns=['Minute', 'Total_kWh'])

    # Write to Excel
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_raw.drop(columns=['Minute'], errors='ignore').to_excel(writer, index=False, sheet_name='Raw Data')
        df_minutely.to_excel(writer, index=False, sheet_name='Minutely Report')

    output.seek(0)
    name = 'full_energy_report.xlsx' if not device_name else f'energy_report_{device_name}.xlsx'
    return send_file(output,
                     as_attachment=True,
                     download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/graph-data')
def api_graph_data_all():
    """Return last 100 rows across all devices (chronological)."""
    results = EnergyData.query.order_by(EnergyData.id.desc()).limit(100).all()
    results = list(reversed(results))
    data = [{
        'timestamp': r.timestamp[11:],  # HH:MM:SS
        'current': round(r.current, 2) if r.current is not None else None,
        'watt': round(r.watt, 2) if r.watt is not None else None,
        'voltage': round(r.voltage, 2) if r.voltage is not None else None,
        'device': r.device_id
    } for r in results]
    return jsonify(data)


@app.route('/api/graph-data/<device_name>')
def api_graph_data_device(device_name):
    """Return last 100 rows for a device (chronological)."""
    results = EnergyData.query.filter_by(device_id=device_name).order_by(EnergyData.id.desc()).limit(100).all()
    results = list(reversed(results))
    data = [{
        'timestamp': r.timestamp[11:],  # HH:MM:SS
        'current': round(r.current, 2) if r.current is not None else None,
        'watt': round(r.watt, 2) if r.watt is not None else None,
        'voltage': round(r.voltage, 2) if r.voltage is not None else None
    } for r in results]
    return jsonify(data)

@app.route('/api/device/<device_name>/power', methods=['POST'])
def control_device_power(device_name):
    """
    Turn a Tuya device ON or OFF.
    Request JSON: { "state": "on" } or { "state": "off" }
    """
    state = request.json.get('state', '').lower()
    if state not in ['on', 'off']:
        return jsonify({"error": "Invalid state"}), 400

    device_info = next((d for d in DEVICES if d["name"] == device_name), None)
    if not device_info:
        return jsonify({"error": "Device not found"}), 404

    try:
        dev = tinytuya.OutletDevice(
            device_info["device_id"],
            device_info["ip"],
            device_info["local_key"]
        )
        dev.set_version(float(device_info.get("protocol", 3.3)))
        dev.set_status(True if state == 'on' else False)

        return jsonify({"success": True, "device": device_name, "state": state})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
