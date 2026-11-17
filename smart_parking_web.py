import json
import base64
import cgi
import io
import os
import sqlite3
import secrets
import time
from datetime import datetime
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

from parking_system import (
    authenticate_guard,
    confirm_pending_detection,
    create_guard_account,
    delete_guard_account,
    delete_parking_event,
    get_dashboard_data,
    get_guard_account,
    get_guard_accounts,
    get_pending_detections,
    dismiss_pending_detection,
    manual_timeout_parking_event,
    reject_pending_detection,
    set_guard_on_duty,
    end_guard_shift,
    settle_guard_shift,
    get_guard_on_duty,
    get_shift_revenue,
    report_incident,
    update_guard_account,
    init_db,
)

HOST = "127.0.0.1"
PORT = 5000
SESSIONS = {}

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _json_response(handler, payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def _html_response(handler, html, status=200):
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def _redirect(handler, location="/"):
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.end_headers()

def _html_escape(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _file_to_data_url(upload):
  if not upload or not getattr(upload, "filename", None):
    return None
  raw_bytes = upload.file.read()
  if not raw_bytes:
    return None
  mime_type = upload.type or "application/octet-stream"
  encoded = base64.b64encode(raw_bytes).decode("ascii")
  return f"data:{mime_type};base64,{encoded}"

def _format_money(value):
    return f"&#8369;{int(value or 0):,}"

def _nice_date(iso_str):
    if not iso_str: return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %Y · %I:%M %p")
    except:
        return _html_escape(iso_str)

def _nice_day_date(iso_str):
  if not iso_str: return "—"
  try:
    dt = datetime.fromisoformat(iso_str)
    return dt.strftime("%a, %b %d, %Y · %I:%M %p")
  except:
    return _html_escape(iso_str)

def _vehicle_label(vehicle_type):
  value = (vehicle_type or "").strip().lower()
  return "MC" if value == "mc" else value.title() if value else "—"

def _guard_active_row(item):
  vtype = item["vehicle_type"] or ""
  tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
  return (f'<tr data-plate="{_html_escape(item["plate_number"]).upper()}">' 
      f'<td><strong>{_html_escape(item["plate_number"])}</strong></td>'
      f'<td><span class="tag {tag}">{_html_escape(item["vehicle_type"])}</span></td>'
      f'<td>{_nice_date(item["entry_time"])}</td>'
      f'<td><button class="btn btn-ghost" style="padding:4px 12px;font-size:12px;" '
      f'onclick="manualTimeout({item["id"]}, \'{_html_escape(item["plate_number"])}\')">⏏ Time Out</button></td></tr>')

def _guard_history_row(item):
  vtype = item["vehicle_type"] or ""
  tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
  return (f'<tr><td><strong>{_html_escape(item["plate_number"])}</strong></td>'
      f'<td><span class="tag {tag}">{_html_escape(item["vehicle_type"])}</span></td>'
      f'<td>{_nice_date(item["entry_time"])}</td>'
      f'<td>{_nice_date(item["exit_time"])}</td>'
      f'<td>{item["duration_minutes"]} min</td>'
      f'<td style="color:var(--teal2);font-weight:700;">{_format_money(item["fee_amount"])}</td></tr>')

def _guard_rejected_row(item):
    vtype = item["vehicle_type"] or ""
    tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
    return (f'<tr><td><strong>{_html_escape(item["plate_number"])}</strong></td>'
            f'<td><span class="tag {tag}">{_html_escape(item["vehicle_type"])}</span></td>'
            f'<td><span class="tag tag-red">Rejected</span></td>'
            f'<td>{_nice_date(item["detected_at"])}</td></tr>')

def _admin_shift_row(item):
  status = item.get("status", "")
  flag_status = item.get("flagged", 0)
  exp_rev = item.get("expected_revenue", 0) if status == "off-duty" else item.get("live_sales", 0)
  act_cash = item.get("actual_cash", 0) if status == "off-duty" else "—"
  notes = _html_escape(item.get("end_notes") or item.get("notes") or "—")
  shift_date = item["shift_start"][:10] if item["shift_start"] else ""
  if status == "on-duty":
    row_bg = "background: rgba(59,158,255,0.08);"
    status_html = '<span class="tag" style="background:rgba(59,158,255,.2);color:#3b9eff;">Ongoing</span>'
  elif flag_status == 1:
    row_bg = "background: rgba(232,64,64,0.08);"
    status_html = f'''<span class="tag" style="background:rgba(232,64,64,.2);color:#ff8080;">⚠️ Flagged</span>
              <form method="post" action="/settle-shift" style="display:inline;margin-left:5px;">
              <input type="hidden" name="shift_id" value="{item['id']}">
              <button type="submit" class="btn btn-ghost" style="padding:2px 8px;font-size:10px;">Settle</button>
              </form>'''
  else:
    row_bg = "background: rgba(24,201,158,0.08);"
    status_html = '<span class="tag" style="background:rgba(24,201,158,.2);color:#18c99e;">✅ Completed/Settled</span>'
  return (f'<tr data-date="{shift_date}" style="{row_bg}"><td><strong>{_html_escape(item["guard_name"])}</strong></td>'
      f'<td>{_nice_date(item["shift_start"])}</td>'
      f'<td>{_nice_date(item.get("shift_end"))}</td>'
      f'<td>{status_html}</td>'
      f'<td style="text-align:center;">{item.get("vehicle_count",0)}</td>'
      f'<td style="color:var(--teal2);font-weight:700;">{_format_money(exp_rev)}</td>'
      f'<td style="color:var(--text);font-weight:700;">{_format_money(act_cash) if act_cash != "—" else "—"}</td>'
      f'<td><div style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{notes}">{notes}</div></td></tr>')

def _admin_history_row(item):
  vtype = item["vehicle_type"] or ""
  tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
  event_date = item["exit_time"][:10] if item.get("exit_time") else item["entry_time"][:10]
  return (f'<tr data-date="{event_date}"><td>{_html_escape(item["guard_name"])}</td>'
      f'<td><strong>{_html_escape(item["plate_number"])}</strong></td>'
      f'<td><span class="tag {tag}">{_html_escape(vtype)}</span></td>'
      f'<td>{_nice_date(item["entry_time"])}</td>'
      f'<td>{_nice_date(item.get("exit_time"))}</td>'
      f'<td style="color:var(--teal2);font-weight:700;">{_format_money(item["fee_amount"])}</td></tr>')

def _admin_rejected_row(item):
  vtype = item["vehicle_type"] or ""
  tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
  rej_date = item["detected_at"][:10] if item.get("detected_at") else ""
  return (f'<tr data-date="{rej_date}"><td><strong>{_html_escape(item["plate_number"])}</strong></td>'
      f'<td><span class="tag {tag}">{_html_escape(vtype)}</span></td>'
      f'<td><span class="tag tag-red">Rejected by Guard</span></td>'
      f'<td>{_nice_date(item["detected_at"])}</td></tr>')

def _incident_row(item):
  return f'<tr><td>{_nice_date(item["reported_at"])}</td><td><strong>{_html_escape(item["guard_name"])}</strong></td><td><span class="tag tag-red">{_html_escape(item["incident_type"])}</span></td><td>{_html_escape(item["notes"])}</td></tr>'

def _parse_payload(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" in content_type:
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(length),
        }
        form = cgi.FieldStorage(fp=io.BytesIO(body), headers=handler.headers, environ=environ, keep_blank_values=True)
        payload = {}
        files = {}
        if form.list:
            for item in form.list:
                if item.filename:
                    files[item.name] = item
                else:
                    payload[item.name] = item.value
        payload["_files"] = files
        return payload
    body_text = body.decode("utf-8")
    if "application/json" in content_type and body_text:
        return json.loads(body_text)
    return {key: values[0] for key, values in parse_qs(body_text).items()}

def _current_user(handler):
    cookie = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
    session = cookie.get("session_id")
    return SESSIONS.get(session.value) if session else None

def _login_response(handler, user, redirect="/"):
    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = user
    handler.send_response(303)
    handler.send_header("Set-Cookie", f"session_id={session_id}; HttpOnly; Path=/; SameSite=Lax")
    handler.send_header("Location", redirect)
    handler.end_headers()

def _logout_response(handler):
    cookie = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
    session = cookie.get("session_id")
    if session:
        SESSIONS.pop(session.value, None)
    handler.send_response(303)
    handler.send_header("Set-Cookie", "session_id=; Max-Age=0; HttpOnly; Path=/; SameSite=Lax")
    handler.send_header("Location", "/login")
    handler.end_headers()

# ── Design system ─────────────────────────────────────────────────────────────

def _fonts():
    return '<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=DM+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">'

def _base_css():
    return """
<style>
:root{
  --bg:#0d1117; --bg2:#161b24; --card:#1a2133; --card2:#1f2840;
  --card-border:rgba(255,255,255,.08); --card-border-hover:rgba(255,255,255,.18);
  --teal:#3b9eff; --teal2:#5bb3ff; --teal-glow:rgba(59,158,255,.25);
  --accent:#f5a623; --red:#e84040; --green:#18c99e;
  --text:#e8edf5; --text2:#8a97b0; --text3:#5a6880;
  --r:14px;--r-sm:10px;--r-lg:20px;--r-xl:26px;
  font-family:'DM Sans',sans-serif; background:var(--bg);color:var(--text);
}
*{box-sizing:border-box;margin:0;padding:0;}
body{min-height:100vh;}
h1,h2,h3,.sora{font-family:'Sora',sans-serif;}
a{color:var(--teal2);text-decoration:none;font-weight:600;}
a:hover{text-decoration:underline;}
input,select,textarea{
  width:100%;padding:11px 14px; border:1.5px solid rgba(255,255,255,.10);
  border-radius:var(--r-sm);font:inherit;font-size:15px;
  background:rgba(255,255,255,.06);color:var(--text);
  transition:border .15s,box-shadow .15s;outline:none;
}
input:focus,select:focus,textarea:focus{border-color:var(--teal);box-shadow:0 0 0 3px var(--teal-glow);}
input::placeholder{color:var(--text3);}
select option{background:#1a2133;color:var(--text);}
label{display:block;font-size:12px;font-weight:700;color:var(--text2);margin-bottom:6px;letter-spacing:.05em;text-transform:uppercase;}
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:12px 22px;border-radius:var(--r-sm);border:none;
  font:600 15px 'DM Sans',sans-serif;cursor:pointer;
  transition:opacity .15s,transform .1s,box-shadow .15s; text-decoration:none;
}
.btn:active{transform:scale(.97);}
.btn-primary{background:linear-gradient(135deg,var(--teal) 0%,#5bb3ff 100%);color:#fff;}
.btn-primary:hover{box-shadow:0 6px 24px var(--teal-glow);opacity:.92;}
.btn-accent{background:var(--accent);color:#09172a;font-weight:700;}
.btn-accent:hover{box-shadow:0 6px 20px rgba(245,166,35,.30);opacity:.92;}
.btn-ghost{background:rgba(255,255,255,.06);color:var(--text2);border:1.5px solid rgba(255,255,255,.10);}
.btn-ghost:hover{background:rgba(255,255,255,.10);color:var(--text);}
.btn-danger{background:var(--red);color:#fff;}
.btn-danger:hover{opacity:.85;}
.btn-full{width:100%;}
.btn-lg{padding:14px 28px;font-size:16px;border-radius:var(--r);}
.field{margin-bottom:18px;}
.error-box{
  background:rgba(232,64,64,.12);border:1.5px solid rgba(232,64,64,.30);
  color:#ff8080;padding:11px 14px;border-radius:var(--r-sm);
  font-size:14px;font-weight:600;margin-bottom:18px;
}
.divider{display:flex;align-items:center;gap:12px;margin:20px 0;color:var(--text3);font-size:13px;}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:rgba(255,255,255,.08);}

/* Layout */
.app-layout { display: flex; height: 100vh; overflow: hidden; background: var(--bg); }
.sidebar {
  width: 280px; flex-shrink: 0; background: var(--card); border-right: 1px solid var(--card-border);
  display: flex; flex-direction: column; padding: 24px; z-index: 10;
}
.main-content { flex: 1; overflow-y: auto; padding: 32px; background: radial-gradient(ellipse 80% 60% at 50% 0%,rgba(30,50,90,.25) 0%,transparent 70%); }
.tab-pane { display: none; animation: fadeIn 0.3s ease-out; }
.tab-pane.active { display: block; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

/* Sidebar components */
.sidebar-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; }
.sidebar-icon { width: 34px; height: 34px; border-radius: 10px; background: linear-gradient(135deg,var(--teal),#1a5fcc); display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 900; color: #fff; font-family: 'Sora', sans-serif; }
.sidebar-name { font-size: 13px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase; color: var(--text); }
.sidebar-nav { display: flex; flex-direction: column; gap: 8px; flex: 1; }
.nav-item {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-radius: var(--r-sm);
  color: var(--text2); font-weight: 600; font-size: 15px; transition: all .15s; text-decoration: none; border: 1px solid transparent;
}
.nav-item:hover { background: rgba(255,255,255,.04); color: var(--text); }
.nav-item.active { background: rgba(59,158,255,.1); color: var(--teal2); border-color: rgba(59,158,255,.2); box-shadow: inset 3px 0 0 var(--teal2); }

.sidebar-bottom { margin-top: auto; padding-top: 24px; border-top: 1px solid var(--card-border); }
.guard-badge { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.guard-avatar { width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; background: linear-gradient(135deg,var(--teal),#1a5fcc); display: flex; align-items: center; justify-content: center; font-family: 'Sora',sans-serif; font-size: 13px; font-weight: 800; color: #fff; }
.guard-name { font-family: 'Sora',sans-serif; font-size: 14px; font-weight: 700; color: var(--text); }
.guard-sub { font-size: 12px; color: var(--text2); margin-top: 2px; }

/* Dashboard UI */
.page-header { margin-bottom: 24px; }
.page-title { font-family: 'Sora',sans-serif; font-size: 26px; font-weight: 800; margin-bottom: 6px; }
.page-sub { color: var(--text2); font-size: 14px; }

.dashboard-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 24px; }
.video-container { background: var(--card); border: 1px solid var(--card-border); border-radius: var(--r); padding: 12px; position: relative; }
.video-badge { position: absolute; top: 24px; left: 24px; display: inline-flex; align-items: center; gap: 6px; background: rgba(24,201,158,.85); color: #fff; font-size: 11px; font-weight: 800; letter-spacing: .06em; padding: 5px 12px; border-radius: 999px; text-transform: uppercase; z-index: 2; backdrop-filter: blur(4px); }
.video-badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #fff; animation: blink 1.5s infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
.live-feed {
  width: 100%; border-radius: var(--r-sm); background: #000;
  aspect-ratio: 16/9; object-fit: cover; display: block;
}

.stats-container { display: grid; grid-template-columns: 1fr; gap: 14px; }
.stat { background: var(--card); border: 1px solid var(--card-border); border-radius: var(--r); padding: 20px; }
.stat-lbl { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--text3); margin-bottom: 8px; }
.stat-val { font-family: 'Sora',sans-serif; font-size: 30px; font-weight: 800; color: var(--text); line-height: 1; }
.stat-hint { font-size: 12px; color: var(--text3); margin-top: 5px; }
.stat.s-accent .stat-val { color: var(--teal2); }

/* Tables and Panels */
.panel{background:var(--card);border:1px solid var(--card-border);border-radius:var(--r);overflow:hidden;margin-bottom:20px;}
.panel-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.06);}
.panel-title{font-family:'Sora',sans-serif;font-size:14px;font-weight:700;color:var(--text);}
.table-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;}
th{padding:10px 16px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);border-bottom:1px solid rgba(255,255,255,.06);text-align:left;white-space:nowrap;background:rgba(255,255,255,.02);}
td{padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.04);font-size:13px;white-space:nowrap;vertical-align:middle;color:var(--text);}
tr:hover td{background:rgba(255,255,255,.03);}
.empty-row{color:var(--text3);text-align:center;padding:28px;}

.tag{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;}
.tag-car{background:rgba(59,158,255,.15);color:var(--teal2);}
.tag-motor{background:rgba(245,166,35,.15);color:#f5a623;}
.tag-red{background:rgba(232,64,64,.15);color:#ff8080;}
.tag-onduty{background:rgba(24,201,158,.15);color:#18c99e;}
.tag-offduty{background:rgba(255,255,255,.07);color:var(--text3);}

/* Screens */
.auth-page, .shift-page{ min-height:100vh;display:flex;align-items:center;justify-content:center; padding:24px; background:radial-gradient(ellipse 80% 60% at 50% 0%,rgba(30,50,90,.55) 0%,transparent 70%),var(--bg); }
.auth-card, .shift-card{ width:min(500px,100%); background:var(--card); border:1px solid var(--card-border); border-radius:var(--r-xl); padding:36px 38px 40px; box-shadow:0 30px 80px rgba(0,0,0,.45); }
.auth-brand{display:flex;align-items:center;gap:12px;margin-bottom:28px;}
.auth-brand-icon{ width:38px;height:38px;border-radius:10px; background:linear-gradient(135deg,var(--teal),#1a5fcc); display:flex;align-items:center;justify-content:center; font-size:18px;font-weight:900;color:#fff; font-family:'Sora',sans-serif;letter-spacing:-.02em; }
.auth-brand-name{font-size:12px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--text2);}

/* Popup — inline below video, no backdrop blur, draggable */
.popup-overlay { display: none; margin-top: 16px; z-index: 10; position: relative; }
.popup-overlay.show { display: block; }
.popup-box {
  width: min(520px, 100%);
  background: var(--card);
  border: 1.5px solid rgba(59,158,255,.35);
  border-radius: var(--r-xl);
  padding: 24px 28px 28px;
  box-shadow: 0 8px 40px rgba(0,0,0,.5), 0 0 0 1px rgba(59,158,255,.1);
  position: relative;
}
.popup-drag-handle {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 14px; cursor: grab; padding-bottom: 12px;
  border-bottom: 1px solid rgba(255,255,255,.06);
}
.popup-drag-handle:active { cursor: grabbing; }
.popup-drag-hint { font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .04em; user-select: none; }
.popup-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 13px;border-radius:999px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;}
.badge-entry{background:rgba(24,201,158,.15);color:#18c99e;}
.badge-exit{background:rgba(245,166,35,.15);color:#f5a623;}
.popup-title{font-family:'Sora',sans-serif;font-size:20px;font-weight:800;color:var(--text);margin-bottom:5px;}
.popup-hint{font-size:14px;color:var(--text2);margin-bottom:18px;}
.popup-actions{display:flex;gap:10px;margin-top:14px;}

/* Video toggle button */
.video-toggle-btn {
  position: absolute; bottom: 20px; right: 20px; z-index: 5;
  background: rgba(0,0,0,.55); border: 1.5px solid rgba(255,255,255,.25);
  color: #fff; font-size: 13px; font-weight: 700; padding: 8px 16px;
  border-radius: 999px; cursor: pointer;
  display: flex; align-items: center; gap: 7px;
  transition: background .2s, border-color .2s;
  font-family: 'DM Sans', sans-serif;
}
.video-toggle-btn:hover { background: rgba(0,0,0,.75); border-color: rgba(255,255,255,.45); }
.video-toggle-btn.paused { background: rgba(232,64,64,.55); border-color: rgba(232,64,64,.7); }

/* Inputs for filters */
.date-filter { width:auto; padding:6px 12px; font-size:13px; background:rgba(255,255,255,0.06); border:1.5px solid rgba(255,255,255,0.1); color:var(--text); border-radius:var(--r-sm); outline:none; }
.date-filter:focus { border-color:var(--teal); }

@media(max-width:900px){ .app-layout { flex-direction: column; } .sidebar { width: 100%; padding: 16px; border-right: none; border-bottom: 1px solid var(--card-border); } .sidebar-nav { flex-direction: row; overflow-x: auto; padding-bottom: 10px; } .dashboard-grid { grid-template-columns: 1fr; } }
</style>
"""

def _page(title, body, extra_head=""):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Smart Parking</title>
{_fonts()}
{_base_css()}
{extra_head}
</head>
<body>{body}</body>
</html>"""


# ── Auth & Shift Flow ─────────────────────────────────────────────────────────

def render_role_select():
    body = """
<div class="auth-page">
  <div class="auth-card">
    <div class="auth-brand">
      <div class="auth-brand-icon">P</div>
      <span class="auth-brand-name">Smart Parking System</span>
    </div>
    <h1 style="font-family:'Sora',sans-serif;font-size:28px;font-weight:800;margin-bottom:6px;">Welcome</h1>
    <p style="font-size:14px;color:var(--text2);margin-bottom:26px;">Select your role to continue.</p>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:22px;">
      <a href="/login?role=guard" style="display:flex;flex-direction:column;align-items:center;gap:10px;padding:24px 16px;border:1.5px solid var(--card-border);border-radius:var(--r);background:var(--card2);text-decoration:none;color:var(--text);transition:all .15s;" onmouseover="this.style.borderColor='var(--teal)';this.style.transform='translateY(-2px)';" onmouseout="this.style.borderColor='var(--card-border)';this.style.transform='none';">
        <div style="font-size:32px;">👮</div>
        <div style="font-family:'Sora',sans-serif;font-size:14px;font-weight:700;">Guard</div>
        <div style="font-size:12px;color:var(--text2);text-align:center;">Shift monitoring &amp;<br>vehicle logging</div>
      </a>
      <a href="/login?role=admin" style="display:flex;flex-direction:column;align-items:center;gap:10px;padding:24px 16px;border:1.5px solid var(--card-border);border-radius:var(--r);background:var(--card2);text-decoration:none;color:var(--text);transition:all .15s;" onmouseover="this.style.borderColor='var(--teal)';this.style.transform='translateY(-2px)';" onmouseout="this.style.borderColor='var(--card-border)';this.style.transform='none';">
        <div style="font-size:32px;">🛡️</div>
        <div style="font-family:'Sora',sans-serif;font-size:14px;font-weight:700;">Admin / Supervisor</div>
        <div style="font-size:12px;color:var(--text2);text-align:center;">Full access &amp; reports</div>
      </a>
    </div>
    <p style="font-size:13px;color:var(--text3);text-align:center;margin-top:18px;">No account? <a href="/signup">Sign up</a></p>
  </div>
</div>
"""
    return _page("Welcome", body)

def render_login(role="guard", error=""):
    is_guard = role == "guard"
    role_label = "Shift Guard" if is_guard else "Admin / Supervisor"
    role_icon = "👮" if is_guard else "🛡️"
    err_html = f'<div class="error-box">⚠️ {_html_escape(error)}</div>' if error else ""

    body = f"""
<div class="auth-page">
  <div class="auth-card">
    <div class="auth-brand"><div class="auth-brand-icon">P</div><span class="auth-brand-name">Smart Parking System</span></div>
    <a href="/login" style="display:inline-flex;font-size:13px;color:var(--text3);margin-bottom:22px;">← Back to role selection</a>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px;"><span style="font-size:26px;">{role_icon}</span><h1 style="font-family:'Sora',sans-serif;font-size:24px;">{role_label} Login</h1></div>
    <p style="font-size:14px;color:var(--text2);margin-bottom:26px;">Enter your credentials to access the system.</p>
    {err_html}
    <form method="post" action="/login">
      <input type="hidden" name="role" value="{role}">
      <div class="field"><label>Username / Employee ID</label><input name="username" required autofocus></div>
      <div class="field"><label>Password</label><input name="password" type="password" required></div>
      <button class="btn btn-primary btn-full btn-lg" type="submit">Login →</button>
    </form>
  </div>
</div>
"""
    return _page("Login", body)

def render_signup(error=""):
    err_html = f'<div class="error-box">⚠️ {_html_escape(error)}</div>' if error else ""
    body = f"""
<div class="auth-page">
  <div class="auth-card">
    <div class="auth-brand"><div class="auth-brand-icon">P</div><span class="auth-brand-name">Smart Parking System</span></div>
    <a href="/login" style="display:inline-flex;font-size:13px;color:var(--text3);margin-bottom:22px;">← Back to login</a>
    <h1 style="font-family:'Sora',sans-serif;font-size:28px;font-weight:800;margin-bottom:6px;">Create Account</h1>
    <p style="font-size:14px;color:var(--text2);margin-bottom:26px;">Register as a guard or administrator.</p>
    {err_html}
    <form method="post" action="/signup">
      <div class="field"><label>Full Name</label><input name="guard_name" placeholder="e.g. Officer Juan Cruz" required></div>
      <div class="field"><label>Username / Employee ID</label><input name="username" placeholder="e.g. officer.cruz" required></div>
      <div class="field"><label>Password</label><input name="password" type="password" placeholder="Choose a strong password" required></div>
      <div class="field">
        <label>Role</label>
        <select name="role">
          <option value="guard">Shift Guard</option>
          <option value="admin">Admin / Supervisor</option>
        </select>
      </div>
      <button class="btn btn-primary btn-full btn-lg" type="submit">Create Account →</button>
    </form>
  </div>
</div>
"""
    return _page("Sign Up", body)

def render_start_shift(user, error=""):
    name = _html_escape(user["guard_name"])
    initials = "".join(w[0].upper() for w in user["guard_name"].split()[:2])
    date_str = datetime.now().strftime("%B %d, %Y")
    err_html = f'<div class="error-box">⚠️ {_html_escape(error)}</div>' if error else ""

    body = f"""
<div class="shift-page">
  <div class="shift-card">
    <div class="auth-brand"><div class="auth-brand-icon">P</div><span class="auth-brand-name">Smart Parking System</span></div>
    <h2 class="sora" style="font-size:22px;font-weight:800;margin-bottom:20px;">Start Your Shift</h2>
    <div class="guard-badge"><div class="guard-avatar">{initials}</div><div><div class="guard-name">{name}</div><div class="guard-sub">You are not currently in an active shift.</div></div></div>
    <div class="meta-row">
      <div class="meta-box"><div class="meta-key">Date</div><div class="meta-val">{date_str}</div></div>
    </div>
    {err_html}
    <form method="post" action="/start-shift">
      <button class="btn btn-primary btn-full btn-lg" type="submit">Confirm Start Shift</button>
    </form>
    <div style="margin-top:16px;text-align:center;"><a href="/logout" style="font-size:13px;color:var(--text3);">← Logout</a></div>
  </div>
</div>
"""
    return _page("Start Shift", body)

def render_end_shift(user, shift_data, expected_revenue, error=""):
    name = _html_escape(user["guard_name"])
    initials = "".join(w[0].upper() for w in user["guard_name"].split()[:2])
    date_str = datetime.now().strftime("%B %d, %Y")
    err_html = f'<div class="error-box">⚠️ {_html_escape(error)}</div>' if error else ""

    body = f"""
<div class="shift-page">
  <div class="shift-card">
    <div class="auth-brand"><div class="auth-brand-icon" style="background:var(--red);">P</div><span class="auth-brand-name">Smart Parking System</span></div>
    <h2 class="sora" style="font-size:22px;font-weight:800;margin-bottom:20px;color:var(--red);">End Your Shift</h2>
    <div class="guard-badge"><div class="guard-avatar" style="background:var(--red);">{initials}</div><div><div class="guard-name">{name}</div><div class="guard-sub">Please review and declare your collected cash.</div></div></div>
    <div class="meta-row">
      <div class="meta-box"><div class="meta-key">Date</div><div class="meta-val">{date_str}</div></div>
    </div>
    {err_html}
    <form method="post" action="/end-shift">
      <div style="background:rgba(59,158,255,.08);border:1.5px solid rgba(59,158,255,.3);border-radius:var(--r-sm);padding:18px;margin-bottom:20px;text-align:center;">
         <div style="font-size:12px;font-weight:700;color:var(--teal2);margin-bottom:6px;">System Expected Revenue</div>
         <div style="font-family:'Sora',sans-serif;font-size:32px;font-weight:800;color:#fff;">{_format_money(expected_revenue)}</div>
      </div>
      <div class="field">
        <label>End Shift Notes</label>
        <textarea name="end_notes" rows="2" placeholder="Report any discrepancies or observations..."></textarea>
      </div>
      <div class="field">
        <label>Actual Cash Collected (₱)</label>
        <input name="actual_cash" type="number" min="0" required style="font-size:20px;font-weight:bold;text-align:center;" placeholder="0">
        <p style="font-size:11px;color:var(--text3);margin-top:6px;">Any mismatch will be flagged for administrative review.</p>
      </div>
      <div style="display:flex;gap:10px;margin-top:10px;">
        <a href="/" class="btn btn-ghost" style="flex:1;">Cancel</a>
        <button class="btn btn-danger" type="submit" style="flex:2;">Confirm End Shift</button>
      </div>
    </form>
  </div>
</div>
"""
    return _page("End Shift", body)

def render_shift_ended(shift_result):
    flagged = shift_result["flagged"]
    status_color = "var(--red)" if flagged else "var(--green)"
    status_text = "⚠️ Flagged for Review" if flagged else "✅ Completed Successfully"
    warning_text = "<p style='color:var(--red);font-size:13px;margin-bottom:15px;'>Variance detected. Please report to the admin immediately.</p>" if flagged else ""
    dt_str = _nice_date(shift_result["shift_end"])

    body = f"""
<div class="shift-page">
  <div style="position:absolute;bottom:30px;left:30px;">
    <a href="/login" class="btn btn-ghost">← Back to Login</a>
  </div>
  <div class="shift-card" style="text-align:center;padding:50px 40px;">
    <div style="font-size:48px;margin-bottom:16px;">{"⚠️" if flagged else "✅"}</div>
    <h1 class="sora" style="font-size:28px;font-weight:800;margin-bottom:8px;">Shift Ended</h1>
    <div style="font-size:15px;font-weight:700;color:{status_color};margin-bottom:16px;">{status_text}</div>
    {warning_text}
    <div style="background:rgba(255,255,255,.04);border-radius:var(--r-sm);padding:16px;margin-bottom:24px;">
      <div style="font-size:12px;color:var(--text3);margin-bottom:4px;">Time & Date</div>
      <div style="font-family:'Sora',sans-serif;font-weight:700;">{dt_str}</div>
    </div>
    <p style="font-size:16px;color:var(--text2);">Thank you for your service.</p>
  </div>
</div>
"""
    return _page("Shift Ended", body)


# ── Dashboard Helper ──────────────────────────────────────────────────────────

def get_vehicle_color(vtype):
    v = vtype.lower()
    if 'car' in v: return '#3b9eff'
    if 'motor' in v or 'mc' in v: return '#f5a623'
    return 'var(--text)'

# ── Page: Guard Dashboard ─────────────────────────────────────────────────────

def render_guard_dashboard(user):
    data = get_dashboard_data()
    name = _html_escape(user["guard_name"])
    initials = "".join(w[0].upper() for w in user["guard_name"].split()[:2])
    
    vehicle_cards = "".join(
        f'<div class="stat"><div class="stat-lbl">{_html_escape(v["vehicle_type"])}</div><div class="stat-val" data-vtype="{_html_escape(v["vehicle_type"])}" style="color: {get_vehicle_color(v["vehicle_type"])};">{v["total"]}</div><div class="stat-hint">currently inside</div></div>'
        for v in data["vehicle_counts"]
    )
    
    def active_row(item):
        vtype = item["vehicle_type"] or ""
        tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
        return (f'<tr><td><strong>{_html_escape(item["plate_number"])}</strong></td>'
                f'<td><span class="tag {tag}">{_html_escape(item["vehicle_type"])}</span></td>'
                f'<td>{_nice_date(item["entry_time"])}</td>'
          f'<td><button class="btn btn-danger" style="padding:4px 12px;font-size:12px;" '
                f'onclick="manualTimeout({item["id"]}, \'{_html_escape(item["plate_number"])}\')">⏏ Time Out</button></td></tr>')
        
    def hist_row(item):
        vtype = item["vehicle_type"] or ""
        tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
        return f'<tr><td><strong>{_html_escape(item["plate_number"])}</strong></td><td><span class="tag {tag}">{_html_escape(item["vehicle_type"])}</span></td><td>{_nice_date(item["entry_time"])}</td><td>{_nice_date(item["exit_time"])}</td><td>{item["duration_minutes"]} min</td><td style="color:var(--teal2);font-weight:700;">{_format_money(item["fee_amount"])}</td></tr>'

    active_rows = "".join(active_row(r) for r in data["active_vehicles"]) or '<tr><td colspan="4" class="empty-row">No active vehicles.</td></tr>'
    hist_rows = "".join(hist_row(r) for r in data["history_events"]) or '<tr><td colspan="6" class="empty-row">No history yet.</td></tr>'
    active_plate_json = json.dumps([row["plate_number"] for row in data["active_vehicles"]])

    body = f"""
<div class="app-layout">
  <aside class="sidebar">
    <div class="sidebar-brand">
      <div class="sidebar-icon">P</div>
      <span class="sidebar-name">Smart Parking</span>
    </div>
    
    <nav class="sidebar-nav">
      <a href="#" class="nav-item active" onclick="switchTab('tab-dashboard', this); return false;">🏠 Dashboard</a>
      <a href="#" class="nav-item" onclick="switchTab('tab-history', this); return false;">📜 History Logs</a>
      <a href="#" class="nav-item" onclick="switchTab('tab-incident', this); return false;">⚠️ Report Incident</a>
    </nav>

    <div class="sidebar-bottom">
      <div class="guard-badge" style="background:transparent;border:none;padding:0;margin-bottom:24px;">
        <div class="guard-avatar">{initials}</div>
        <div>
          <div class="guard-name" id="sidebarGuardName">{name}</div>
          <div class="guard-sub"><span style="color:var(--green);">●</span> On Duty</div>
        </div>
      </div>
      <a href="/end-shift" class="btn btn-danger btn-full" style="margin-bottom:10px;">End Shift</a>
      <a href="/logout" class="btn btn-ghost btn-full">Logout</a>
    </div>
  </aside>

  <main class="main-content">
    
    <div id="tab-dashboard" class="tab-pane active">
      <div class="page-header">
        <h1 class="page-title">Monitoring Feed</h1>
        <p class="page-sub">Live video feed and real-time statistics.</p>
      </div>
      
      <!-- Main dashboard layout: video | popup | stats -->
      <div style="display:grid;grid-template-columns:2fr 1fr;gap:20px;margin-bottom:20px;">
        <!-- Left: video -->
        <div class="video-container">
          <div class="video-badge">Live View</div>
          <img id="liveVideo" class="live-feed" src="/video_feed" alt="Live annotated feed">
          <button class="video-toggle-btn" id="videoToggleBtn" onclick="toggleFeed()">
            ⏸ Pause Feed
          </button>
        </div>
        
        <!-- Right: popup area (top) + stats (bottom) -->
        <div style="display:flex;flex-direction:column;gap:14px;">

          <!-- Incoming vehicle popup — fixed to this slot, no drag, X = silent reject -->
          <div id="popup" style="display:none;">
            <div class="popup-box" style="padding:20px 22px 22px;">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.06);">
                <span id="actionBadge" class="popup-badge badge-entry">🚗 Incoming vehicle</span>
                <button type="button" onclick="silentDismissPopup()" style="background:rgba(255,255,255,.08);border:1.5px solid rgba(255,255,255,.15);color:var(--text2);width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:14px;line-height:1;display:flex;align-items:center;justify-content:center;" title="Dismiss (reject silently)">✕</button>
              </div>
              <h2 class="popup-title" id="popupTitle" style="font-size:17px;margin-bottom:4px;">Incoming Vehicle</h2>
              <p style="font-size:13px;color:var(--text2);margin-bottom:14px;">AI detected this plate. Confirm or correct it before logging.</p>
              <form id="confirmForm">
                <input type="hidden" id="pendingId">
                <div class="field" style="margin-bottom:10px;">
                  <label>Plate Number</label>
                  <input id="plateInput" required style="font-family:'Sora',sans-serif;font-size:18px;font-weight:700;letter-spacing:.08em;">
                </div>
                <div style="font-size:12px;color:var(--text2);margin-bottom:12px;" id="vehicleInfo"></div>
                <div style="display:flex;gap:8px;">
                  <button class="btn btn-primary" type="submit" style="flex:1;padding:10px;">✅ Confirm</button>
                  <button class="btn btn-danger" type="button" onclick="rejectPending()" style="flex:1;padding:10px;">✕ Reject</button>
                </div>
              </form>
            </div>
          </div>

          <!-- Stats: vehicles inside + sales today side by side -->
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
            <div class="stat">
              <div class="stat-lbl">Vehicles inside</div>
              <div class="stat-val" data-stat="inside" style="font-size:26px;">{data["inside_count"]}</div>
            </div>
            <div class="stat s-accent">
              <div class="stat-lbl">Sales today</div>
              <div class="stat-val" data-stat="sales" style="color:#18c99e;font-size:22px;">{_format_money(data["total_sales_today"])}</div>
            </div>
          </div>

          <!-- Per-vehicle-type cards -->
          {vehicle_cards}
        </div>
      </div>
    </div>

    <div id="tab-incident" class="tab-pane">
      <div class="page-header">
        <h1 class="page-title">Report Incident</h1>
        <p class="page-sub">Log any anomalies, security issues, or system errors.</p>
      </div>
      <div style="display:flex;justify-content:center;align-items:flex-start;min-height:60vh;padding-top:20px;">
        <div class="panel" style="width:100%;max-width:580px;padding:28px 32px;">
          <div style="margin-bottom:20px;">
            <div style="font-size:13px;color:var(--text3);font-weight:600;">DATE</div>
            <div style="font-size:15px;font-weight:700;margin-bottom:8px;">{datetime.now().strftime("%B %d, %Y")}</div>
            <div style="font-size:13px;color:var(--text3);font-weight:600;">TIME</div>
            <div style="font-size:15px;font-weight:700;">{datetime.now().strftime("%I:%M %p")}</div>
          </div>
          <form method="post" action="/report-incident">
            <div class="field">
              <label>Incident Type</label>
              <select name="incident_type">
                <option value="System Glitch">System Glitch (Camera/Gate)</option>
                <option value="Customer Dispute">Customer Dispute</option>
                <option value="Security Issue">Security Issue</option>
                <option value="Other">Other</option>
              </select>
            </div>
            <div class="field">
              <label>Notes / Description</label>
              <textarea name="notes" rows="5" required placeholder="Describe what happened..."></textarea>
            </div>
            <div style="display:flex;justify-content:center;">
              <button class="btn btn-primary" type="submit" style="min-width:180px;">Submit Report</button>
            </div>
          </form>
        </div>
      </div>
    </div>

    <div id="tab-history" class="tab-pane">
      <div class="page-header">
        <h1 class="page-title">History Logs</h1>
        <p class="page-sub">Current vehicles and completed time-outs.</p>
      </div>

      <!-- Dropdown tabs for history sections -->
      <div style="display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap;">
        <button class="btn btn-primary hist-tab-btn" onclick="switchHistTab('hist-active', this)" style="font-size:13px;padding:8px 16px;">Active Inside (Time-in)</button>
        <button class="btn btn-ghost hist-tab-btn" onclick="switchHistTab('hist-timeout', this)" style="font-size:13px;padding:8px 16px;">History Logs (Time-out)</button>
      </div>

      <div id="hist-active" class="hist-tab-pane">
        <div class="panel">
          <div class="panel-head"><span class="panel-title">Active Inside (Time-in)</span></div>
          <div class="table-wrap"><table><thead><tr><th>Plate</th><th>Type</th><th>Time In</th><th>Action</th></tr></thead><tbody id="guardActiveBody">{active_rows}</tbody></table></div>
        </div>
      </div>

      <div id="hist-timeout" class="hist-tab-pane" style="display:none;">
        <div class="panel">
          <div class="panel-head"><span class="panel-title">History Logs (Time-out)</span></div>
          <div class="table-wrap"><table><thead><tr><th>Plate</th><th>Type</th><th>Time In</th><th>Time Out</th><th>Duration</th><th>Payment</th></tr></thead><tbody id="guardHistoryBody">{hist_rows}</tbody></table></div>
        </div>
      </div>
    </div>

  </main>
</div>

<!-- Manual Timeout Modal -->
<div id="timeoutModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:none;align-items:center;justify-content:center;">
  <div class="popup-box" style="width:min(420px,90vw);">
    <h2 class="popup-title" style="margin-bottom:6px;">Manual Time-Out</h2>
    <p style="font-size:13px;color:var(--text2);margin-bottom:16px;">Confirm time-out for plate <strong id="timeoutPlate"></strong>. Collect payment before confirming.</p>
    <div style="display:flex;gap:10px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="closeTimeoutModal()">Cancel</button>
      <button class="btn btn-primary" style="flex:2;" onclick="confirmManualTimeout()">✅ Confirm Time-Out</button>
    </div>
  </div>
</div>

<script>
let _timeoutEventId = null;

function manualTimeout(eventId, plate) {{
  _timeoutEventId = eventId;
  document.getElementById('timeoutPlate').textContent = plate;
  const modal = document.getElementById('timeoutModal');
  modal.style.display = 'flex';
}}

function closeTimeoutModal() {{
  document.getElementById('timeoutModal').style.display = 'none';
  _timeoutEventId = null;
}}

async function confirmManualTimeout() {{
  if (!_timeoutEventId) return;
  const r = await fetch('/api/manual_timeout', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{event_id: _timeoutEventId}})
  }});
  const result = await r.json();
  closeTimeoutModal();
  if (result.fee_amount !== undefined)
    alert('✅ Time-out recorded.\\nFee: ₱' + result.fee_amount + '\\nDuration: ' + result.duration_minutes + ' min');
  location.reload();
}}

function switchHistTab(tabId, btn) {{
  document.querySelectorAll('.hist-tab-pane').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.hist-tab-btn').forEach(el => {{
    el.classList.remove('btn-primary');
    el.classList.add('btn-ghost');
  }});
  document.getElementById(tabId).style.display = 'block';
  btn.classList.remove('btn-ghost');
  btn.classList.add('btn-primary');
}}

function switchTab(tabId, element) {{
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  element.classList.add('active');
}}

function normalizePlate(value) {{
  return (value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
}}

let activePlates = new Set({active_plate_json}.map(normalizePlate));

function syncPopupMode() {{
  const plateInput = document.getElementById('plateInput');
  const title = document.getElementById('popupTitle');
  const badge = document.getElementById('actionBadge');
  if (!plateInput || !title || !badge) return;
  const matchedExit = activePlates.has(normalizePlate(plateInput.value));
  title.textContent = matchedExit ? 'Exiting Vehicle' : 'Incoming Vehicle';
  badge.textContent = matchedExit ? '🚙 Exiting vehicle' : '🚗 Incoming vehicle';
  badge.className = 'popup-badge ' + (matchedExit ? 'badge-exit' : 'badge-entry');
}}

const plateInputEl = document.getElementById('plateInput');
if (plateInputEl) plateInputEl.addEventListener('input', syncPopupMode);

// ── Freeze-on-pause ──────────────────────────────────────────────────────
let feedPaused = false;
const _img = document.getElementById('liveVideo');
const _canvas = document.createElement('canvas');
_canvas.className = 'live-feed';
_canvas.style.display = 'none';
_img.parentNode.insertBefore(_canvas, _img.nextSibling);

async function toggleFeed() {{
  const btn = document.getElementById('videoToggleBtn');
  feedPaused = !feedPaused;
  if (feedPaused) {{
    _canvas.width  = _img.naturalWidth  || _img.offsetWidth;
    _canvas.height = _img.naturalHeight || _img.offsetHeight;
    _canvas.getContext('2d').drawImage(_img, 0, 0, _canvas.width, _canvas.height);
    _img.style.display    = 'none';
    _canvas.style.display = 'block';
    btn.textContent = '▶ Resume Feed';
    btn.classList.add('paused');
    await fetch('/api/pause_detection');
  }} else {{
    _canvas.style.display = 'none';
    _img.style.display    = 'block';
    _img.src = '/video_feed?' + Date.now();
    btn.textContent = '⏸ Pause Feed';
    btn.classList.remove('paused');
    await fetch('/api/resume_detection');
  }}
}}

// ── Live stats refresh ──────────────────────────────────────────────────
async function refreshStats() {{
  try {{
    const r = await fetch('/api/stats');
    if (!r.ok) return;
    const d = await r.json();
    document.querySelector('[data-stat="inside"]').textContent = d.inside_count;
    document.querySelector('[data-stat="sales"]').innerHTML = '&#8369;' + parseInt(d.total_sales_today || 0).toLocaleString();
    if (d.vehicle_counts) {{
      d.vehicle_counts.forEach(function(v) {{
        const el = document.querySelector('[data-vtype="' + v.vehicle_type + '"]');
        if (el) el.textContent = v.total;
      }});
    }}
    if (d.guard_active_rows) document.getElementById('guardActiveBody').innerHTML = d.guard_active_rows;
    if (d.guard_history_rows) document.getElementById('guardHistoryBody').innerHTML = d.guard_history_rows;
    if (d.guard_rejected_rows) document.getElementById('guardRejectedBody').innerHTML = d.guard_rejected_rows;
    if (d.guard_active_plates) activePlates = new Set(d.guard_active_plates.map(normalizePlate));
  }} catch(e) {{}}
}}
setInterval(refreshStats, 3000);

// ── Pending detection polling ────────────────────────────────────────────
let activePending = null;
let pollStarted   = false;

function startPolling() {{
  if (pollStarted) return;
  pollStarted = true;
  setInterval(pollPending, 2000);
}}

_img.addEventListener('load', function onFirstFrame() {{
  startPolling();
  _img.removeEventListener('load', onFirstFrame);
}});
setTimeout(startPolling, 5000);

async function pollPending() {{
  try {{
    const r = await fetch('/api/pending');
    if (!r.ok) return;
    const items = await r.json();
    if (items.length && !activePending) showPending(items[0]);
  }} catch(e) {{}}
}}

function showPending(item) {{
  activePending = item;
  document.getElementById('pendingId').value  = item.id;
  document.getElementById('plateInput').value = item.plate_number || '';
  const isExit = item.action === 'exit' || activePlates.has(normalizePlate(item.plate_number));
  document.getElementById('popupTitle').textContent = isExit ? 'Exiting Vehicle' : 'Incoming Vehicle';
  const badge = document.getElementById('actionBadge');
  badge.textContent = isExit ? '🚙 Exiting vehicle' : '🚗 Incoming vehicle';
  badge.className   = 'popup-badge ' + (isExit ? 'badge-exit' : 'badge-entry');
  document.getElementById('vehicleInfo').textContent =
    'Type: ' + (item.vehicle_type || '—') +
    ' | AI Read Time: ' + (item.detected_at || '').replace('T', ' ');
  document.getElementById('popup').style.display = 'block';
  syncPopupMode();
}}

// ── Confirm ──────────────────────────────────────────────────────────────
document.getElementById('confirmForm').addEventListener('submit', async function(e) {{
  e.preventDefault();
  const r = await fetch('/api/confirm', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      pending_id:   document.getElementById('pendingId').value,
      plate_number: document.getElementById('plateInput').value
    }})
  }});
  const result = await r.json();
  if (result.action === 'exit')
    alert('✅ Payment collected: ₱' + result.fee_amount + '\\nDuration: ' + result.duration_minutes + ' minutes');
  activePending = null;
  document.getElementById('popup').style.display = 'none';
  await refreshStats();
}});

async function rejectPending() {{
  await fetch('/api/reject', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{pending_id: document.getElementById('pendingId').value}})
  }});
  activePending = null;
  document.getElementById('popup').style.display = 'none';
}}

// X button — silently dismiss (no reject log), keep pending in DB
async function silentDismissPopup() {{
  await fetch('/api/reject_silent', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{pending_id: document.getElementById('pendingId').value}})
  }});
  activePending = null;
  document.getElementById('popup').style.display = 'none';
}}
</script>
"""
    return _page("Guard Dashboard", body)


# ── Page: Admin Dashboard ─────────────────────────────────────────────────────

def render_admin_dashboard(user):
    data = get_dashboard_data()
    name = _html_escape(user["guard_name"])
    initials = "".join(w[0].upper() for w in user["guard_name"].split()[:2])

    vehicle_cards = "".join(
    f'<div class="stat"><div class="stat-lbl">{_html_escape(v["vehicle_type"])}</div><div class="stat-val" data-vtype="{_html_escape(v["vehicle_type"])}" style="color: {get_vehicle_color(v["vehicle_type"])};">{v["total"]}</div><div class="stat-hint">currently inside</div></div>'
    for v in data["vehicle_counts"]
  )

    def shift_row(item):
        status = item.get("status","")
        flag_status = item.get("flagged", 0)
        exp_rev = item.get("expected_revenue", 0) if status == "off-duty" else item.get("live_sales", 0)
        act_cash = item.get("actual_cash", 0) if status == "off-duty" else "—"
        notes = _html_escape(item.get("end_notes") or item.get("notes") or "—")
        shift_date = item["shift_start"][:10] if item["shift_start"] else ""

        if status == "on-duty":
            row_bg = "background: rgba(59,158,255,0.08);"
            status_html = '<span class="tag" style="background:rgba(59,158,255,.2);color:#3b9eff;">Ongoing</span>'
        elif flag_status == 1:
            row_bg = "background: rgba(232,64,64,0.08);"
            status_html = f''' <span class="tag" style="background:rgba(232,64,64,.2);color:#ff8080;">⚠️ Flagged</span>
                               <form method="post" action="/settle-shift" style="display:inline;margin-left:5px;">
                                 <input type="hidden" name="shift_id" value="{item['id']}">
                                 <button type="submit" class="btn btn-ghost" style="padding:2px 8px;font-size:10px;">Settle</button>
                               </form>'''
        else:
            row_bg = "background: rgba(24,201,158,0.08);"
            status_html = '<span class="tag" style="background:rgba(24,201,158,.2);color:#18c99e;">✅ Completed/Settled</span>'

        return (f'<tr data-date="{shift_date}" style="{row_bg}"><td><strong>{_html_escape(item["guard_name"])}</strong></td>'
                f'<td>{_nice_day_date(item["shift_start"])}</td>'
                f'<td>{_nice_day_date(item.get("shift_end"))}</td>'
                f'<td>{status_html}</td>'
                f'<td style="text-align:center;">{item.get("vehicle_count",0)}</td>'
                f'<td style="color:var(--teal2);font-weight:700;">{_format_money(exp_rev)}</td>'
                f'<td style="color:var(--text);font-weight:700;">{_format_money(act_cash) if act_cash != "—" else "—"}</td>'
                f'<td><div style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{notes}">{notes}</div></td></tr>')

    def history_row(item):
        vtype = item["vehicle_type"] or ""
        tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
        event_date = item["exit_time"][:10] if item.get("exit_time") else item["entry_time"][:10]
        
        return (f'<tr data-date="{event_date}"><td>{_html_escape(item["guard_name"])}</td>'
                f'<td><strong>{_html_escape(item["plate_number"])}</strong></td>'
                f'<td><span class="tag {tag}">{_html_escape(vtype)}</span></td>'
                f'<td>{_nice_date(item["entry_time"])}</td>'
                f'<td>{_nice_date(item.get("exit_time"))}</td>'
                f'<td style="color:var(--teal2);font-weight:700;">{_format_money(item["fee_amount"])}</td></tr>')

    def admin_rej_row(item):
        vtype = item["vehicle_type"] or ""
        tag = "tag-car" if "car" in vtype.lower() else "tag-motor"
        rej_date = item["detected_at"][:10] if item.get("detected_at") else ""
        return (f'<tr data-date="{rej_date}">'
                f'<td><strong>{_html_escape(item["plate_number"])}</strong></td>'
                f'<td><span class="tag {tag}">{_html_escape(vtype)}</span></td>'
                f'<td><span class="tag tag-red">Rejected by Guard</span></td>'
                f'<td>{_nice_date(item["detected_at"])}</td></tr>')

    # compute latest shift per guard for quick lookup in accounts table
    guard_latest = {}
    for s in data.get("guard_shift_sales", []) or []:
        gname = s.get("guard_name")
        if not gname: continue
        cur = guard_latest.get(gname)
        if not cur or (s.get("shift_start") or "") > (cur.get("shift_start") or ""):
            guard_latest[gname] = s

    def account_row(item):
      # Skip admin accounts — only show guards in this table
      if (item.get("role") or "guard") == "admin":
        return ''
      # last login/logout/status/revenue from latest shift (if any)
      latest = guard_latest.get(item.get("guard_name"))
      last_login = _nice_day_date(latest.get("shift_start")) if latest and latest.get("shift_start") else '—'
      last_logout = _nice_day_date(latest.get("shift_end")) if latest and latest.get("shift_end") else '—'
      status = latest.get("status") if latest else ''
      flag_status = latest.get("flagged", 0) if latest else 0
      if status == 'on-duty':
        status_html = '<span class="tag" style="background:rgba(59,158,255,.2);color:#3b9eff;">Ongoing</span>'
      elif flag_status == 1:
        status_html = '<span class="tag" style="background:rgba(232,64,64,.2);color:#ff8080;">⚠️ Flagged</span>'
      else:
        status_html = '<span class="tag" style="background:rgba(24,201,158,.2);color:#18c99e;">✅ Completed/Settled</span>'
      revenue_val = 0
      if latest:
        if latest.get('status') == 'off-duty':
          revenue_val = latest.get('expected_revenue') or latest.get('actual_cash') or 0
        else:
          revenue_val = latest.get('live_sales') or 0
      revenue = _format_money(revenue_val)
      return (
        f'<tr data-account-id="{item["id"]}">'
        f'<td><strong>{_html_escape(item["guard_name"])}</strong></td>'
        f'<td>{last_login}</td>'
        f'<td>{last_logout}</td>'
        f'<td>{status_html}</td>'
        f'<td style="color:var(--teal2);font-weight:700;">{revenue}</td>'
        f'<td>'
        f'<form method="post" action="/account-delete" onsubmit="return confirm(\'Delete this guard account?\')" style="display:inline;">'
        f'<input type="hidden" name="account_id" value="{item["id"]}">'
        f'<button type="submit" class="btn btn-danger" style="padding:6px 10px;font-size:12px;">Delete</button>'
        f'</form></td></tr>'
      )

    def incident_row(item):
        return f'<tr><td>{_nice_date(item["reported_at"])}</td><td><strong>{_html_escape(item["guard_name"])}</strong></td><td><span class="tag tag-red">{_html_escape(item["incident_type"])}</span></td><td>{_html_escape(item["notes"])}</td></tr>'

    shift_rows = "".join(shift_row(r) for r in data["guard_shift_sales"]) or '<tr><td class="empty-row" colspan="8">No guard shifts recorded yet.</td></tr>'
    hist_rows = "".join(history_row(r) for r in data["history_events"]) or '<tr><td class="empty-row" colspan="6">No payment history yet.</td></tr>'
    account_rows = "".join(account_row(r) for r in data.get("guard_accounts", [])) or '<tr><td class="empty-row" colspan="6">No guard accounts found.</td></tr>'
    incident_rows = "".join(incident_row(r) for r in data.get("incidents", [])) or '<tr><td class="empty-row" colspan="4">No incidents reported yet.</td></tr>'

    body = f"""
<div class="app-layout">
  <aside class="sidebar">
    <div class="sidebar-brand">
      <div class="sidebar-icon">P</div>
      <span class="sidebar-name">Smart Parking</span>
    </div>
    
    <nav class="sidebar-nav">
      <a href="#" class="nav-item active" onclick="switchTab('tab-admin-overview', this); return false;">📊 Overview</a>
      <a href="#" class="nav-item" onclick="switchTab('tab-admin-shifts', this); return false;">👮 Guard Shifts</a>
      <a href="#" class="nav-item" onclick="switchTab('tab-admin-history', this); return false;">📜 Payment History</a>
      <a href="#" class="nav-item" onclick="switchTab('tab-admin-incidents', this); return false;">⚠️ Incident Reports</a>
      <a href="#" class="nav-item" onclick="switchTab('tab-admin-manage-guards', this); return false;">🗂️ Manage Accounts</a>
    </nav>

    <div class="sidebar-bottom">
      <div class="guard-badge" style="background:transparent;border:none;padding:0;margin-bottom:24px;">
        <div class="guard-avatar" style="background:linear-gradient(135deg,#6a2fcf,#1a5fcc);">{initials}</div>
        <div>
          <div class="guard-name" id="sidebarAdminName">{name}</div>
          <div class="guard-sub">Administrator</div>
        </div>
      </div>
      <a href="/logout" class="btn btn-ghost btn-full">Logout</a>
    </div>
  </aside>

  <main class="main-content">
    
    <div id="tab-admin-overview" class="tab-pane active">
      <div class="page-header">
        <h1 class="page-title">Admin Dashboard</h1>
        <p class="page-sub">Monitor system performance and guard accountability.</p>
      </div>
      
      <div class="dashboard-grid">
        <div class="video-container">
          <div class="video-badge">Live View</div>
          <img id="liveVideo" class="live-feed" src="/video_feed" alt="Live annotated feed">
          <button class="video-toggle-btn" id="videoToggleBtn" onclick="toggleFeed()">
            ⏸ Pause Feed
          </button>
        </div>
        
        <div class="stats-container">
          <div class="stat">
            <div class="stat-lbl">Total sales today</div>
            <div class="stat-val" data-stat="admin-sales" style="color: #18c99e;">{_format_money(data["total_sales_today"])}</div>
            <div class="stat-hint">all guards combined</div>
          </div>
          <div class="stat">
            <div class="stat-lbl">Vehicles inside now</div>
            <div class="stat-val" data-stat="admin-inside">{data["inside_count"]}</div>
            <div class="stat-hint">currently parked</div>
          </div>
          {vehicle_cards}
        </div>
      </div>
    </div>

    <div id="tab-admin-shifts" class="tab-pane">
      <div class="page-header">
        <h1 class="page-title">Guard Shifts</h1>
        <p class="page-sub">Review shift audits, collected cash, and flagged variances.</p>
      </div>

      <div id="admin-shift-logs" class="admin-shift-pane">
        <div class="panel">
          <div class="panel-head" style="display:flex; justify-content:space-between; align-items:center;">
              <span class="panel-title">Shift Logs</span>
              <input type="date" id="shiftDate" class="date-filter">
          </div>
          <div class="table-wrap">
            <table id="shiftTable">
              <thead><tr><th>Guard</th><th>Login Time</th><th>Logout Time</th><th>Status</th><th style="text-align:center;">Vehicles Paid</th><th>Revenue</th><th>Actual Cash</th><th>Notes</th></tr></thead>
              <tbody>{shift_rows}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div id="tab-admin-manage-guards" class="tab-pane">
      <div class="page-header">
        <h1 class="page-title">Manage Accounts</h1>
        <p class="page-sub">View and delete guard accounts.</p>
      </div>
      <div class="panel">
        <div class="panel-head"><span class="panel-title">Guard Accounts</span></div>
        <div class="table-wrap">
          <table id="guardAccountsTable">
            <thead><tr><th>Full Name</th><th>Last Login</th><th>Last Logout</th><th>Status</th><th>Revenue</th><th>Actions</th></tr></thead>
            <tbody>{account_rows}</tbody>
          </table>
        </div>
      </div>
      <div class="panel" style="padding:24px;">
        <h3 class="sora" style="font-size:16px;font-weight:700;margin-bottom:20px;color:var(--text2);">Create New Guard Account</h3>
        <form method="post" action="/account-save" id="adminAccountForm">
          <input type="hidden" name="account_id" id="adminAccountId">
          <div class="field"><label>Full Name</label><input name="guard_name" id="adminGuardName" required></div>
          <div class="field"><label>Username</label><input name="username" id="adminUsername" required></div>
          <div class="field">
            <label>Role</label>
            <select name="role" id="adminRole">
              <option value="guard">Guard</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          <div class="field"><label>Password</label><input type="password" name="password" id="adminPassword" placeholder="Set a password for this account" required></div>
          <div style="display:flex;gap:10px;justify-content:center;">
            <button class="btn btn-ghost" type="button" onclick="clearAccountEditor()" style="min-width:120px;">Clear</button>
            <button class="btn btn-primary" type="submit" style="min-width:180px;">Create Account</button>
          </div>
        </form>
      </div>
    </div>

    <div id="tab-admin-history" class="tab-pane">
      <div class="page-header">
        <h1 class="page-title">Payment History</h1>
        <p class="page-sub">Complete log of all vehicle exits and transactions.</p>
      </div>

      <div class="panel">
        <div class="panel-head" style="display:flex; justify-content:space-between; align-items:center;">
            <span class="panel-title">Transaction Records (Exits)</span>
            <input type="date" id="historyDate" class="date-filter">
        </div>
        <div class="table-wrap">
          <table id="historyTable">
            <thead><tr><th>Guard</th><th>Plate</th><th>Type</th><th>Login Time</th><th>Logout Time</th><th>Revenue</th></tr></thead>
            <tbody id="adminHistoryBody">{hist_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
    
    <div id="tab-admin-incidents" class="tab-pane">
      <div class="page-header">
        <h1 class="page-title">Incident Reports</h1>
        <p class="page-sub">Security issues, anomalies, and logs reported by guards.</p>
      </div>
      <div class="panel">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Date & Time</th><th>Guard</th><th>Type</th><th>Notes</th></tr></thead>
            <tbody>{incident_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

  </main>
</div>

<script>
function switchTab(tabId, element) {{
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  element.classList.add('active');
}}

function switchAdminHistTab(tabId, btn) {{
  document.querySelectorAll('.admin-hist-pane').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.admin-hist-tab-btn').forEach(el => {{
    el.classList.remove('btn-primary');
    el.classList.add('btn-ghost');
  }});
  document.getElementById(tabId).style.display = 'block';
  btn.classList.remove('btn-ghost');
  btn.classList.add('btn-primary');
}}

function clearAccountEditor() {{
  document.getElementById('adminAccountId').value = '';
  document.getElementById('adminGuardName').value = '';
  document.getElementById('adminUsername').value = '';
  document.getElementById('adminPassword').value = '';
  document.getElementById('adminRole').value = 'guard';
}}

// ── Video feed toggle ──────────────────────────────────────────────────────
let feedPaused = false;
async function toggleFeed() {{
  const img = document.getElementById('liveVideo');
  const btn = document.getElementById('videoToggleBtn');
  feedPaused = !feedPaused;
  img.src = feedPaused ? '' : '/video_feed?' + Date.now();
  btn.textContent = feedPaused ? '▶ Resume Feed' : '⏸ Pause Feed';
  btn.classList.toggle('paused', feedPaused);
  await fetch(feedPaused ? '/api/pause_detection' : '/api/resume_detection');
}}

// ── Live stats refresh (every 3 s) ────────────────────────────────────────
async function refreshAdminStats() {{
  try {{
    const r = await fetch('/api/stats');
    if (!r.ok) return;
    const d = await r.json();
    const insideEl = document.querySelector('[data-stat="admin-inside"]');
    if (insideEl) insideEl.textContent = d.inside_count;
    const salesEl = document.querySelector('[data-stat="admin-sales"]');
    if (salesEl) salesEl.innerHTML = '&#8369;' + parseInt(d.total_sales_today || 0).toLocaleString();
    if (d.vehicle_counts) {{
      d.vehicle_counts.forEach(function(v) {{
        const el = document.querySelector('[data-vtype="' + v.vehicle_type + '"]');
        if (el) el.textContent = v.total;
      }});
    }}
    if (d.admin_shift_rows) document.getElementById('shiftTable').querySelector('tbody').innerHTML = d.admin_shift_rows;
    if (d.admin_history_rows) document.getElementById('adminHistoryBody').innerHTML = d.admin_history_rows;
    if (d.admin_rejected_rows) document.getElementById('adminRejectedBody').innerHTML = d.admin_rejected_rows;
  }} catch(e) {{}}
}}
setInterval(refreshAdminStats, 3000);

// ── Calendar filters ───────────────────────────────────────────────────────
document.getElementById('shiftDate').addEventListener('change', function() {{
    const selectedDate = this.value;
    document.querySelectorAll('#shiftTable tbody tr').forEach(row => {{
        if(!selectedDate) {{ row.style.display = ''; }}
        else {{ row.style.display = row.getAttribute('data-date') === selectedDate ? '' : 'none'; }}
    }});
}});

document.getElementById('historyDate').addEventListener('change', function() {{
    const selectedDate = this.value;
    document.querySelectorAll('#historyTable tbody tr').forEach(row => {{
        if(!selectedDate) {{ row.style.display = ''; }}
        else {{ row.style.display = row.getAttribute('data-date') === selectedDate ? '' : 'none'; }}
    }});
    document.querySelectorAll('#rejectedTable tbody tr').forEach(row => {{
        if(!selectedDate) {{ row.style.display = ''; }}
        else {{ row.style.display = row.getAttribute('data-date') === selectedDate ? '' : 'none'; }}
    }});
}});
</script>
"""
    return _page("Admin Dashboard", body)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class SmartParkingHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        user = _current_user(self)
        path = self.path.split("?")[0]
        query = self.path[len(path):]

        if path == "/login" and not query:
            _html_response(self, render_role_select())
            return

        if path == "/login":
            params = parse_qs(query.lstrip("?"))
            role = params.get("role", ["guard"])[0]
            _html_response(self, render_login(role))
            return

        if path == "/signup":
            _html_response(self, render_signup())
            return

        if path == "/logout":
            _logout_response(self)
            return

        # ── MJPEG annotated video stream ──────────────────────────────────────
        if path == "/video_feed":
            from app import get_latest_frame
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    frame_bytes = get_latest_frame()
                    if frame_bytes:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(frame_bytes)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.04)   # ~25 fps
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        # ─────────────────────────────────────────────────────────────────────

        if path == "/api/pending":
            if not user or user["role"] != "guard":
                _json_response(self, [], 403)
                return
            _json_response(self, get_pending_detections())
            return

        # ── Stats endpoint (soft refresh after confirm) ───────────────────────
        if path == "/api/stats":
            data = get_dashboard_data()
            _json_response(self, {
                "inside_count": data["inside_count"],
                "total_sales_today": int(data["total_sales_today"] or 0),
                "vehicle_counts": data["vehicle_counts"],
            "guard_active_plates": [row["plate_number"] for row in data["active_vehicles"]],
            "guard_active_rows": "".join(_guard_active_row(r) for r in data["active_vehicles"]) or '<tr><td colspan="4" class="empty-row">No active vehicles.</td></tr>',
            "guard_history_rows": "".join(_guard_history_row(r) for r in data["history_events"]) or '<tr><td colspan="6" class="empty-row">No history yet.</td></tr>',
            "admin_shift_rows": "".join(_admin_shift_row(r) for r in data["guard_shift_sales"]) or '<tr><td class="empty-row" colspan="8">No guard shifts recorded yet.</td></tr>',
            "admin_history_rows": "".join(_admin_history_row(r) for r in data["history_events"]) or '<tr><td class="empty-row" colspan="6">No payment history yet.</td></tr>',
            })
            return
        # ─────────────────────────────────────────────────────────────────────

        # ── Detection pause/resume ────────────────────────────────────────────
        if path == "/api/pause_detection":
            from app import pause_detection
            pause_detection()
            _json_response(self, {"paused": True})
            return

        if path == "/api/resume_detection":
            from app import resume_detection
            resume_detection()
            _json_response(self, {"paused": False})
            return
        # ─────────────────────────────────────────────────────────────────────

        # ── Raw video file ────────────────────────────────────────────────────
        if path == "/test.mp4":
            video_path = "test.mp4"
            try:
                file_size = os.path.getsize(video_path)
                range_header = self.headers.get("Range", None)

                if range_header:
                    byte_range = range_header.strip().replace("bytes=", "")
                    start_str, end_str = byte_range.split("-")
                    start = int(start_str)
                    end = int(end_str) if end_str else file_size - 1
                    length = end - start + 1

                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()

                    with open(video_path, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(file_size))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()

                    with open(video_path, "rb") as f:
                        self.wfile.write(f.read())
            except FileNotFoundError:
                _json_response(self, {"error": "motor.mp4 not found"}, 404)
            return
        # ─────────────────────────────────────────────────────────────────────

        if not user:
            _redirect(self, "/login")
            return

        if path == "/end-shift":
            on_duty = get_guard_on_duty()
            if not on_duty or on_duty["guard_name"] != user["guard_name"]:
                _redirect(self, "/")
                return
            expected_rev = get_shift_revenue(user["guard_name"], on_duty["shift_start"])
            _html_response(self, render_end_shift(user, on_duty, expected_rev))
            return

        if path == "/" or path == "":
            if user["role"] == "admin":
                _html_response(self, render_admin_dashboard(user))
            else:
                on_duty = get_guard_on_duty()
                if on_duty and on_duty["guard_name"] == user["guard_name"]:
                    _html_response(self, render_guard_dashboard(user))
                else:
                    _html_response(self, render_start_shift(user))
            return

        _json_response(self, {"error": "Not found"}, 404)

    def do_POST(self):
        payload = _parse_payload(self)
        path = self.path.split("?")[0]

        if path == "/signup":
            try:
                user = create_guard_account(
                    payload.get("guard_name"), payload.get("username"),
                    payload.get("password"), payload.get("role", "guard"),
                )
                _login_response(self, user, "/")
            except sqlite3.IntegrityError:
                _html_response(self, render_signup("Username already exists."), 400)
            except ValueError as e:
                _html_response(self, render_signup(str(e)), 400)
            except Exception as e:
                _html_response(self, render_signup(f"Error: {str(e)}"), 500)
            return

        if path == "/login":
            role = payload.get("role", "guard")
            user = authenticate_guard(payload.get("username"), payload.get("password"))
            if not user:
                _html_response(self, render_login(role, "No credentials found. Please contact the administrator."), 401)
                return
            if user["role"] != role:
                _html_response(self, render_login(role, f"This account is registered as '{user['role']}', not '{role}'."), 403)
                return
            _login_response(self, user, "/")
            return

        if path == "/start-shift":
            user = _current_user(self)
            if not user: return _redirect(self, "/login")
            set_guard_on_duty(user["guard_name"], "")
            _redirect(self, "/")
            return
            
        if path == "/end-shift":
            user = _current_user(self)
            if not user or user["role"] != "guard": return _redirect(self, "/")
            
            result = end_guard_shift(user["guard_name"], payload.get("actual_cash", 0), payload.get("end_notes", ""))
            if result:
                cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                session = cookie.get("session_id")
                if session: SESSIONS.pop(session.value, None)
                
                body = render_shift_ended(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Set-Cookie", "session_id=; Max-Age=0; HttpOnly; Path=/; SameSite=Lax")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                _redirect(self, "/")
            return
            
        if path == "/settle-shift":
            user = _current_user(self)
            if not user or user["role"] != "admin": return _redirect(self, "/login")
            settle_guard_shift(payload.get("shift_id"))
            _redirect(self, "/")
            return

        if path == "/report-incident":
            user = _current_user(self)
            if not user: return _redirect(self, "/login")
            report_incident(user["guard_name"], payload.get("incident_type", "Other"), payload.get("notes", ""))
            _redirect(self, "/")
            return

        if path == "/api/confirm":
            user = _current_user(self)
            if user and user["role"] == "guard":
                event = confirm_pending_detection(payload.get("pending_id"), payload.get("plate_number"), user["guard_name"])
                _json_response(self, event)
            return

        if path == "/api/reject":
            user = _current_user(self)
            if user and user["role"] == "guard":
                reject_pending_detection(payload.get("pending_id"))
                _json_response(self, {"ok": True})
            return

        if path == "/api/reject_silent":
          user = _current_user(self)
          if user and user["role"] == "guard":
            dismiss_pending_detection(payload.get("pending_id"))
            _json_response(self, {"ok": True})
          return

        if path == "/api/manual_timeout":
          user = _current_user(self)
          if user and user["role"] == "guard":
            result = manual_timeout_parking_event(payload.get("event_id"), user["guard_name"])
            _json_response(self, result or {"ok": False}, 200 if result else 404)
          return

        if path == "/account-save":
          user = _current_user(self)
          if not user:
            _redirect(self, "/login")
            return

          account_id = payload.get("account_id") or user.get("id")
          is_admin = user.get("role") == "admin"
          files = payload.get("_files", {})
          uploaded_picture = _file_to_data_url(files.get("profile_picture_file"))
          if not account_id:
            if not is_admin:
              _redirect(self, "/")
              return
            created = create_guard_account(
              payload.get("guard_name"),
              payload.get("username"),
              payload.get("password"),
              payload.get("role", "guard"),
            )
            if created:
              picture = uploaded_picture or (payload.get("profile_picture") or "").strip() or None
              if picture:
                update_guard_account(created["id"], created["guard_name"], created["username"], None, created.get("role"), picture)
              _redirect(self, "/")
            else:
              _redirect(self, "/")
            return

          target = get_guard_account(account_id)
          if not target:
            _redirect(self, "/")
            return
          if not is_admin and int(account_id) != int(user["id"]):
            _redirect(self, "/")
            return

          password = payload.get("password") or None
          role = payload.get("role") if is_admin else None
          profile_picture = uploaded_picture or (payload.get("profile_picture") or "").strip() or None
          updated = update_guard_account(
            account_id,
            payload.get("guard_name"),
            payload.get("username"),
            password,
            role,
            profile_picture,
          )
          if updated and not is_admin and int(account_id) == int(user["id"]):
            cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            session = cookie.get("session_id")
            if session and session.value in SESSIONS:
              SESSIONS[session.value] = {
                "id": updated["id"],
                "guard_name": updated["guard_name"],
                "username": updated["username"],
                "role": updated["role"],
                "profile_picture": updated.get("profile_picture"),
              }
          _redirect(self, "/")
          return

        if path == "/account-delete":
          user = _current_user(self)
          if not user or user.get("role") != "admin":
            _redirect(self, "/")
            return

          account_id = payload.get("account_id")
          if not account_id:
            _redirect(self, "/")
            return

          target = get_guard_account(account_id)
          if not target:
            _redirect(self, "/")
            return

          delete_guard_account(account_id)
          cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
          session = cookie.get("session_id")
          if session and int(target["id"]) == int(user["id"]):
            SESSIONS.pop(session.value, None)
            self.send_response(303)
            self.send_header("Set-Cookie", "session_id=; Max-Age=0; HttpOnly; Path=/; SameSite=Lax")
            self.send_header("Location", "/login")
            self.end_headers()
            return

          _redirect(self, "/")
          return

        if path == "/delete-event":
            user = _current_user(self)
            if user and user["role"] == "admin":
                delete_parking_event(payload.get("event_id"))
                _redirect(self, "/")
            return

        _json_response(self, {"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    init_db()

    # ── Start plate detection on the video feed ───────────────────────────
    from app import start_detection_thread
    start_detection_thread()          # runs in background, feeds DB + MJPEG stream
    # ─────────────────────────────────────────────────────────────────────

    server = ThreadingHTTPServer((HOST, PORT), SmartParkingHandler)
    print(f"Smart Parking running at http://{HOST}:{PORT}")
    print("Press CTRL+C to stop.")
    server.serve_forever()