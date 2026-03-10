# MeshCore Email Gateway

Bridge the gap between traditional email and the [MeshCore](https://github.com/liamcottle/meshcore) mesh network. This gateway allows you to send and receive messages from MeshCore RF nodes using ordinary email, and comes with a web‑based management interface for easy configuration, monitoring, and debugging.

## Features

- **Bidirectional communication**  
  - **Email → RF**: Send an email to a special address; the subject (6‑char hex node prefix) determines the destination MeshCore node, and the body becomes the RF message.  
  - **RF → Email**: Incoming RF messages starting with `#` followed by an email address are forwarded to that address via SMTP.

- **Flexible connection to MeshCore devices**  
  - BLE (Bluetooth Low Energy)  
  - Serial port (UART)  
  - TCP (with auto‑reconnect)

- **IMAP IDLE monitoring**  
  - Listens for new emails in real time and processes them immediately.

- **Customisable error and reply templates**  
  - All delivery failure messages and the RF‑to‑email reply format are defined in the configuration and can be freely edited.

- **Live status and logging**  
  - WebSocket connection pushes real‑time status, statistics, and logs to any connected web client.

- **RESTful management API**  
  - Configure, start/stop the gateway, test SMTP, manually send messages, and more.

- **Built‑in static frontend**  
  - Place your compiled frontend in the `frontend/` folder and it will be served automatically.

## Installation

### Prerequisites
- Python 3.9 or higher
- A MeshCore device (or use simulation mode for testing)
- An email account with SMTP and IMAP access (e.g., Gmail, QQ Mail)

### Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/MeshCore-Email-Gateway.git
   cd MeshCore-Email-Gateway
   ```

2. **Create a virtual environment (recommended)**
   ```bash
   python -m venv venv
   source venv/bin/activate   # Linux/macOS
   venv\Scripts\activate      # Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   If you don’t have a `requirements.txt` yet, generate one:
   ```bash
   pip install fastapi uvicorn aioimaplib pydantic
   # optional: if you have MeshCore Python bindings, install them too
   ```

4. **Configure the gateway**  
   Edit `config.json` (it will be created automatically with defaults on first run). See [Configuration](#configuration) below.

5. **Prepare the frontend (optional)**  
   Place your built frontend files in the `frontend/` directory. The server will serve `index.html` from there. If no frontend is found, the API root returns a simple JSON message.

6. **Run the server**
   ```bash
   python server.py
   ```
   The server will start at `http://localhost:8765`. Open this URL in your browser to access the management interface.

## Configuration

All settings are stored in `config.json`. The first time you run the gateway, a default configuration is created. Below is a description of each field.

### Connection settings
| Field | Description |
|-------|-------------|
| `connection_type` | `"ble"`, `"serial"`, or `"tcp"` |
| `ble_address` | BLE MAC address of the MeshCore device (optional) |
| `ble_pin` | PIN for BLE pairing (if required) |
| `serial_port` | Serial port, e.g. `"/dev/ttyUSB0"` or `"COM3"` |
| `serial_baudrate` | Baud rate for serial communication |
| `tcp_host` | IP/hostname of the TCP‑connected MeshCore device |
| `tcp_port` | TCP port |
| `tcp_auto_reconnect` | Whether to automatically reconnect on TCP disconnect |

### Email settings
| Field | Description |
|-------|-------------|
| `agent_email` | The email address that the gateway uses (from address for outgoing mails) |
| `smtp_server` | SMTP server hostname |
| `smtp_port` | SMTP port (usually 587 for STARTTLS, 465 for SSL) |
| `smtp_user` | SMTP username (usually the full email address) |
| `smtp_password` | SMTP password or app‑specific password |
| `smtp_use_tls` | Use STARTTLS (true) or plain (false) |
| `imap_server` | IMAP server hostname |
| `imap_port` | IMAP port (usually 993 for SSL) |
| `imap_user` | IMAP username |
| `imap_password` | IMAP password |

### RF & misc
| Field | Description |
|-------|-------------|
| `rf_max_message_bytes` | Maximum allowed length (in bytes) for an RF message (including sender email and body) |
| `idle_timeout` | IMAP IDLE timeout in seconds (renewal interval) |
| `debug` | Enable verbose debug logging |
| `error_formats` | Dictionary of templates for various error/notification emails. See below. |

### Error / Reply templates
The `error_formats` key holds sub‑dictionaries for each situation. The following keys are used:

- `invalid-subject` – subject line not a 6‑char hex prefix.
- `node-not-found` – no contact matches the prefix.
- `msg-too-long` – message exceeds RF limit.
- `delivery-fail` – RF transmission failed.
- `rf-reply` – format for forwarding an RF message to an email.

Each template can contain `subject` and `body` strings with placeholders like `{{node_name}}`, `{{node_prefix}}`, `{{rf_message}}`, `{{gateway_email}}`, etc. See the default configuration in `server.py` for examples.

## Usage

### Starting / Stopping the gateway
- Use the web interface (if available) or call the REST API:
  - `POST /api/connect` – start the gateway (connects to MeshCore and IMAP)
  - `POST /api/disconnect` – stop the gateway

### Sending an RF message via email
1. Compose an email to the gateway’s monitored inbox.
2. Set the subject to the **6‑character hex prefix** of the target MeshCore node (e.g., `a1b2c3`).
3. Write your message in the email body.
4. Send the email. The gateway will forward it to the RF node.

### Receiving email from an RF node
1. On your MeshCore device, send a message starting with `#` followed by an email address, e.g.:
   ```
   #user@example.com Hello from the mesh!
   ```
2. The gateway will forward this message to `user@example.com` using the configured SMTP server.

### Manual send via API
You can also trigger a message programmatically:
```http
POST /api/send_email
Content-Type: application/json

{
  "to_prefix": "a1b2c3",
  "from_email": "sender@example.com",
  "body": "Hello node!"
}
```
Requires the gateway to be running.

## API Reference

The backend provides a RESTful API at `/api/*`. Below are the most important endpoints.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET/POST | Get or update configuration. |
| `/api/connect` | POST | Start the gateway (connects to MeshCore and IMAP). |
| `/api/disconnect` | POST | Stop the gateway. |
| `/api/status` | GET | Return current running status, stats, and contacts. |
| `/api/contacts` | GET | Return list of known MeshCore contacts. |
| `/api/logs` | GET | Return recent log entries (max 200). |
| `/api/test/smtp` | POST | Send a test email to `agent_email` to verify SMTP settings. |
| `/api/send_email` | POST | Manually send a message to an RF node. |

## WebSocket

Connect to `ws://localhost:8765/ws` to receive real‑time updates. On connection you will receive an initial payload containing current status, stats, logs, and contacts. Subsequent messages are pushed for new logs, status changes, and statistics updates.

Message format is JSON with a `type` field:
- `init` – initial data dump
- `log` – new log entry (`time`, `level`, `message`)
- `status` – status update (`running`, `rf`, `imap`, `simulation`, optional `error`)
- `stats` – updated statistics

## Simulation mode

If the `meshcore` Python package is not installed, the gateway runs in simulation mode. In this mode, no actual RF communication occurs; all sends are logged and treated as successful. This is useful for testing the email side without a physical device.

## Docker

A Dockerfile is not provided by default, but you can easily containerise the application:

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "server.py"]
```

Build and run:
```bash
docker build -t mesh-email-gateway .
docker run -p 8765:8765 -v $(pwd)/config.json:/app/config.json mesh-email-gateway
```

## Troubleshooting

- **IMAP login fails** – Many email providers require an **app‑specific password** instead of your regular password. For Gmail, enable 2‑factor authentication and generate an app password. For QQ Mail, use the authorisation code.
- **SMTP connection refused** – Check if your provider blocks SMTP on the default ports; try port 587 with STARTTLS.
- **RF messages not arriving** – Verify that the MeshCore device is correctly paired/connected and that the destination node is in range.

## License

This project is licensed under the MIT License – see the [LICENSE](LICENSE) file for details.

---

*Happy meshing!*