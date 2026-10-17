import sqlite3
import os
from datetime import datetime
from flask import Flask, request, redirect, render_template_string, send_file, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("KSP_CRM_SECRET_KEY", "change-this-local-crm-secret-key")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ksp_bio.db")


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            packing TEXT NOT NULL,
            stock REAL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE,
            order_date TEXT,
            customer TEXT,
            phone TEXT,
            address TEXT,
            state TEXT,
            pincode TEXT,
            advisor TEXT,
            product TEXT,
            quantity REAL DEFAULT 0,
            rate REAL DEFAULT 0,
            subtotal REAL DEFAULT 0,
            discount REAL DEFAULT 0,
            gst REAL DEFAULT 0,
            grand_total REAL DEFAULT 0,
            order_status TEXT DEFAULT 'Pending',
            payment_status TEXT DEFAULT 'Pending',
            tracking_id TEXT,
            weight TEXT,
            post_status TEXT DEFAULT 'Pending',
            delivery_status TEXT DEFAULT 'Pending',
            credit_received TEXT DEFAULT 'No',
            invoice_no TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER UNIQUE,
            date TEXT, farmer_name TEXT, mob_no TEXT, alternate TEXT,
            add1 TEXT, add2 TEXT, pin TEXT, product TEXT, total_amount TEXT,
            advance TEXT, lable TEXT, advisor_name TEXT, tracking_id TEXT,
            confirmation TEXT DEFAULT 'Pending', remarks TEXT, status TEXT,
            delivery_comment TEXT, order_status TEXT DEFAULT 'Pending',
            call_date TEXT, credited TEXT
        )
    """)
    lead_cols = {row[1] for row in conn.execute('PRAGMA table_info(leads)').fetchall()}
    if 'order_id' not in lead_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN order_id INTEGER")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            packing TEXT,
            quantity REAL DEFAULT 0,
            rate REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    # Add new columns safely to existing databases without deleting orders.
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(orders)').fetchall()}
    if 'delivery_status' not in existing_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN delivery_status TEXT DEFAULT 'Pending'")
    if 'credit_received' not in existing_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN credit_received TEXT DEFAULT 'No'")

    # Login accounts used for access control and edit-history attribution.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Safely migrate older users tables without deleting existing accounts.
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "active" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        user_cols.add("active")

    # Older versions may store the password in a column named password, not password_hash.
    # Add password_hash and migrate existing values so login can use secure hash checking.
    if "password_hash" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "password" in user_cols:
            old_users = conn.execute("SELECT id, password FROM users").fetchall()
            for old_user in old_users:
                old_password = old_user["password"]
                if old_password:
                    if str(old_password).startswith(("pbkdf2:", "scrypt:")):
                        password_hash = old_password
                    else:
                        password_hash = generate_password_hash(str(old_password))
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, old_user["id"]))
        user_cols.add("password_hash")

    # Create a first admin account only when the users table is empty. Change this password after first login.
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, 'admin', 1)",
            ("admin", generate_password_hash("KspBio@2026"))
        )

    # Seed the known KSP BIO team accounts if they do not already exist.
    # Existing accounts/passwords are never overwritten. Change these temporary passwords after first login.
    known_employees = [
        ("sangram", "Ksp@Sangram26"),
        ("prachi", "Ksp@Prachi26"),
        ("akshay", "Ksp@Akshay26"),
        ("ashwini", "Ksp@Ashwini26"),
        ("shweta", "Ksp@Shweta26"),
        ("yogesh", "Ksp@Yogesh26"),
        ("aditya", "Ksp@Aditya26"),
        ("komal", "Ksp@Komal26"),
        ("vaishnavi", "Ksp@Vaishnavi26"),
        ("rohit", "Ksp@Rohit26"),
    ]
    for employee_id, temporary_password in known_employees:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role, active) VALUES (?, ?, 'staff', 1)",
            (employee_id, generate_password_hash(temporary_password))
        )

    # Keep intentionally deleted Order Sheet links deleted across app restarts.
    conn.execute("""CREATE TABLE IF NOT EXISTS deleted_order_sheet_links (order_id INTEGER PRIMARY KEY)""")

    # Backfill orders created before the Order Sheet link existed, except rows explicitly deleted from Order Sheet.
    conn.execute("""
        INSERT INTO leads (order_id, date, farmer_name, mob_no, add1, pin, product,
                           total_amount, advisor_name, tracking_id, confirmation, order_status)
        SELECT o.id, o.order_date, o.customer, o.phone, o.address, o.pincode, o.product,
               o.grand_total, o.advisor, o.tracking_id, 'Pending', COALESCE(o.order_status,'Pending')
        FROM orders o
        WHERE NOT EXISTS (SELECT 1 FROM deleted_order_sheet_links d WHERE d.order_id = o.id)
          AND NOT EXISTS (
            SELECT 1 FROM leads l
            WHERE (l.order_id = o.id)
               OR (COALESCE(l.date,'') = COALESCE(o.order_date,'')
                   AND COALESCE(l.farmer_name,'') = COALESCE(o.customer,'')
                   AND COALESCE(l.mob_no,'') = COALESCE(o.phone,''))
        )
    """)
    conn.commit()
    conn.close()


create_tables()

@app.before_request
def require_login():
    allowed_endpoints = {"login", "static"}
    if request.endpoint in allowed_endpoints or request.endpoint is None:
        return None
    if not session.get("username"):
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    return None

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["username"] = user["username"]
            session["role"] = user["role"]
            target = request.form.get("next", "")
            if not target.startswith("/") or target.startswith("//") or target.startswith("/login"):
                target = "/"
            return redirect(target)
        error = "Username kiwa password chukicha aahe."
    return render_template_string("""
    <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>KSP BIO CRM Login</title><style>body{font-family:Arial;background:#eef4f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}.box{background:white;padding:30px;border-radius:12px;box-shadow:0 5px 24px #ccd5ce;width:min(360px,90vw)}h2{color:#176b45;margin-top:0}label{display:block;font-weight:bold;margin-top:14px}input{width:100%;box-sizing:border-box;padding:11px;margin-top:6px;border:1px solid #ccd5ce;border-radius:6px}button{width:100%;padding:12px;margin-top:20px;background:#176b45;color:white;border:0;border-radius:6px;font-weight:bold;cursor:pointer}.err{color:#b91c1c}</style></head><body><div class="box"><h2>KSP BIO CRM</h2><p>Employee Login / Admin Login</p>{% if error %}<p class="err">{{error}}</p>{% endif %}<form method="post"><input type="hidden" name="next" value="{{next}}"><label>Employee ID</label><input name="username" autocomplete="username" required><label>Password</label><input type="password" name="password" autocomplete="current-password" required><button type="submit">Login</button></form></div></body></html>
    """, error=error, next=request.args.get("next", "/"))

@app.route("/settings", methods=["GET", "POST"])
def settings():
    # Only an admin can create and manage employee IDs.
    if session.get("role") != "admin":
        return "<h2>Access denied</h2><p>Employee accounts fakt Admin manage karu shakto.</p><p><a href='/'>Dashboard</a></p>", 403

    message = ""
    if request.method == "POST":
        action = request.form.get("action", "add")
        conn = get_db()
        if action == "add":
            employee_id = request.form.get("employee_id", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "staff")
            if not employee_id or not password:
                message = "Employee ID ani password donhi bhar."
            elif len(password) < 6:
                message = "Password kamit kami 6 characters cha pahije."
            elif role not in ("admin", "staff"):
                message = "Role invalid aahe."
            else:
                try:
                    conn.execute("INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, ?, 1)",
                                 (employee_id, generate_password_hash(password), role))
                    conn.commit()
                    message = f"Employee ID {employee_id} create zala. Ata Employee Login madhun login karta yeil."
                except sqlite3.IntegrityError:
                    message = "Ha Employee ID aadhich aahe. Dusra ID vapra."
        elif action == "toggle":
            user_id = request.form.get("user_id", type=int)
            if user_id:
                user = conn.execute("SELECT id, username, role, active FROM users WHERE id=?", (user_id,)).fetchone()
                if user and user["username"] != session.get("username"):
                    conn.execute("UPDATE users SET active=? WHERE id=?", (0 if user["active"] else 1, user_id))
                    conn.commit()
                    message = "Employee account status update zala."
                else:
                    message = "Swatahcha admin account ithe disable karta yenar nahi."
        conn.close()

    conn = get_db()
    employees = conn.execute("SELECT id, username, role, active FROM users ORDER BY role DESC, username").fetchall()
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Employee Management & Settings</title>
    <style>body{font-family:Arial;background:#f4f6f8;margin:0;color:#1f2937;font-size:17px}.box{max-width:900px;margin:28px auto;background:white;padding:26px;border-radius:12px;box-shadow:0 2px 10px #ddd}h2{color:#176b45}label{display:block;margin-top:12px;font-weight:bold}input,select{padding:10px;width:100%;box-sizing:border-box;margin-top:5px;border:1px solid #bbb;border-radius:6px}button{padding:10px 16px;background:#176b45;color:white;border:0;border-radius:6px;margin-top:14px;cursor:pointer}.msg{background:#e8f5ee;padding:12px;border-radius:7px}.warn{color:#a11}table{width:100%;border-collapse:collapse;margin-top:18px}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}a{color:#176b45;font-weight:bold}.nav a{margin-right:14px}</style></head><body><div class="box">
    <h2>Employee Management / Settings</h2><p>Admin panel: navin Employee ID create kara ani accounts manage kara.</p>
    {% if message %}<p class="msg">{{message}}</p>{% endif %}
    <h3>New Employee Add Kara</h3><form method="post"><input type="hidden" name="action" value="add">
    <label>Employee ID / Username</label><input name="employee_id" placeholder="Udaharan: EMP001" required>
    <label>Password (minimum 6 characters)</label><input type="password" name="password" minlength="6" required>
    <label>Role</label><select name="role"><option value="staff">Employee / Staff</option><option value="admin">Admin</option></select>
    <button type="submit">Create Employee ID</button></form>
    <h3>Existing Accounts</h3><table><tr><th>Employee ID</th><th>Role</th><th>Status</th><th>Action</th></tr>
    {% for e in employees %}<tr><td>{{e['username']}}</td><td>{{e['role']}}</td><td>{{'Active' if e['active'] else 'Disabled'}}</td><td>{% if e['username'] != current_user %}<form method="post" style="margin:0;padding:0;box-shadow:none"><input type="hidden" name="action" value="toggle"><input type="hidden" name="user_id" value="{{e['id']}}"><button type="submit">{{'Disable' if e['active'] else 'Enable'}}</button></form>{% else %}Current account{% endif %}</td></tr>{% endfor %}
    </table><p class="nav"><a href="/">Dashboard</a> <a href="/leads">Order Sheet</a> <a href="/logout">Logout</a></p>
    </div></body></html>
    """, message=message, employees=employees, current_user=session.get("username", ""))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

STYLE = """
<style>
body {font-family:Arial,sans-serif;background:#f4f6f8;margin:0;color:#1f2937}
.header {background:#1f2937;color:white;padding:18px 28px;font-size:24px;font-weight:bold}
.container {padding:24px;max-width:1200px;margin:auto}
nav {padding:14px 28px;background:#e5e7eb}
nav a {margin-right:18px;color:#1f2937;text-decoration:none;font-weight:bold}
.card, form {background:white;padding:22px;border-radius:10px;box-shadow:0 2px 8px #ddd;margin-bottom:20px}
.grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
label {font-weight:bold;display:block}
input,select,textarea {width:100%;box-sizing:border-box;padding:10px;margin-top:6px;margin-bottom:14px;border:1px solid #cbd5e1;border-radius:5px}
button,.btn {display:inline-block;padding:11px 17px;background:#1f2937;color:white;border:0;border-radius:5px;text-decoration:none;cursor:pointer}
button.remove {background:#b91c1c;padding:8px 12px}
.section {font-size:19px;font-weight:bold;margin:20px 0 12px;border-bottom:1px solid #ddd;padding-bottom:8px}
table {border-collapse:collapse;width:100%;background:white;margin-top:18px}
th,td {border:1px solid #d1d5db;padding:10px;text-align:left}
th {background:#e5e7eb}
.product-row {display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:10px;align-items:center}
.totalbox {font-size:18px;font-weight:bold;margin:12px 0}
.small {color:#64748b;font-size:13px}
@media(max-width:700px){.grid{grid-template-columns:1fr}.product-row{grid-template-columns:1fr}.container{padding:12px}}

/* INVENTRA-INSPIRED LEFT SIDEBAR */

:root { --sidebar:#176B45; --sidebar2:#E8F5EE; --accent:#D1FAE5; --page:#F7FAF8; }
html, body { min-height:100%; }
body { background:var(--page); color:#1e2d26; }
.header {
  position:fixed !important; z-index:30; left:0; top:0; bottom:auto;
  width:260px; height:86px; box-sizing:border-box; padding:24px 22px;
  background:var(--sidebar); color:#fff; font-size:22px; font-weight:800;
  border:0; display:block; letter-spacing:.2px;
}
.header:after { content:'BUSINESS MANAGEMENT'; display:block; margin-top:5px; color:#b9d7c7; font-size:10px; font-weight:600; letter-spacing:1.6px; }
nav {
  position:fixed !important; z-index:29; left:0; top:86px; bottom:0;
  width:260px; box-sizing:border-box; padding:22px 13px;
  background:var(--sidebar); display:flex; flex-direction:column; align-items:stretch;
  gap:7px; overflow-y:auto; border:0; box-shadow:5px 0 20px rgba(15,40,29,.08);
}
nav:before { content:'WORKSPACE'; display:block; padding:0 13px 9px; color:#8eae9d; font-size:10px; font-weight:800; letter-spacing:1.6px; }
nav a { display:flex; align-items:center; gap:12px; margin:0; padding:13px 14px; border-radius:9px; color:#dbe9e1; text-decoration:none; font-size:14px; font-weight:600; transition:background .15s ease,color .15s ease; }
nav a:before { content:'›'; display:inline-flex; align-items:center; justify-content:center; width:22px; height:22px; border-radius:6px; background:rgba(255,255,255,.08); color:#c8f58c; font-size:20px; line-height:1; }
nav a:hover, nav a:focus { background:#235a40; color:#fff; }
.container { margin-left:260px !important; max-width:none !important; width:auto; padding:32px 36px !important; }
.container h1 { color:#183d2c; margin-top:4px; font-size:29px; letter-spacing:-.5px; }
.card, form { border:1px solid #e1e9e3; border-radius:14px; box-shadow:0 5px 18px rgba(18,61,40,.055); }
button,.btn { background:#1a7047; border-radius:8px; font-weight:700; }
button:hover,.btn:hover { background:#145536; }
th { background:#e5f1e8; color:#143d29; }
table { border-radius:10px; overflow:hidden; }
th,td { padding:11px 12px; }
input:focus,select:focus,textarea:focus { outline:2px solid #a7d8b7; border-color:#328452; }
@media(max-width:600px) {
  .header { position:relative !important; left:auto; top:auto; bottom:auto; width:auto; height:auto; padding:17px 16px; font-size:20px; }
  .header:after { margin-top:3px; }
  nav { position:relative !important; left:auto; top:auto; bottom:auto; width:auto; padding:10px; flex-direction:row; flex-wrap:wrap; gap:5px; }
  nav:before { display:none; }
  nav a { padding:9px 10px; }
  .container { margin-left:0 !important; padding:16px !important; }
}
@media print {
  .header, nav { display:none !important; }
  .container { margin:0 !important; padding:0 !important; }
  body { background:#fff !important; }
}

/* GLOBAL READABILITY: larger text across all CRM pages */
body { font-size:17px !important; line-height:1.55; }
.container { font-size:17px; }
.container h1 { font-size:32px !important; }
.container h2 { font-size:25px; }
.container h3 { font-size:25px; }
nav a { font-size:16px !important; padding:14px 15px !important; }
label { font-size:16px !important; }
input, select, textarea { font-size:16px !important; padding:12px !important; }
button, .btn { font-size:16px !important; padding:12px 18px !important; }
table { font-size:16px !important; }
th, td { padding:13px 14px !important; }
.section { font-size:22px !important; }
.totalbox { font-size:21px !important; }
.small { font-size:15px !important; }
@media(max-width:600px) {
  body { font-size:16px !important; }
  .container h1 { font-size:27px !important; }
  nav a { font-size:14px !important; }
  table { font-size:14px !important; }
  th, td { padding:9px !important; }
}
</style>
"""


@app.route("/")
def dashboard():
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    current_month = datetime.now().strftime("%Y-%m")
    selected_month = request.args.get("month", current_month).strip()
    # Accept only YYYY-MM month values; fall back safely to the current month.
    try:
        datetime.strptime(selected_month, "%Y-%m")
    except ValueError:
        selected_month = current_month

    total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    today_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE substr(order_date,1,10)=?", (today,)).fetchone()[0]
    pending_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE COALESCE(order_status,'Pending')='Pending'").fetchone()[0]
    delivered_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE COALESCE(delivery_status,'Pending')='Delivered'").fetchone()[0]
    returned_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE COALESCE(delivery_status,'Pending')='Returned'").fetchone()[0]
    pending_deliveries = conn.execute("SELECT COUNT(*) FROM orders WHERE COALESCE(delivery_status,'Pending')='Pending'").fetchone()[0]
    dispatched_deliveries = conn.execute("SELECT COUNT(*) FROM orders WHERE COALESCE(delivery_status,'Pending')='Dispatched'").fetchone()[0]
    total_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    total_stock = conn.execute("SELECT COALESCE(SUM(stock),0) FROM products").fetchone()[0]
    low_stock = conn.execute("SELECT COUNT(*) FROM products WHERE COALESCE(stock,0) BETWEEN 1 AND 10").fetchone()[0]
    out_of_stock = conn.execute("SELECT COUNT(*) FROM products WHERE COALESCE(stock,0)<=0").fetchone()[0]
    today_sales = conn.execute("SELECT COALESCE(SUM(grand_total),0) FROM orders WHERE substr(order_date,1,10)=?", (today,)).fetchone()[0]
    overall_sales = conn.execute("SELECT COALESCE(SUM(grand_total),0) FROM orders").fetchone()[0]
    month_sales = conn.execute("SELECT COALESCE(SUM(grand_total),0) FROM orders WHERE substr(order_date,1,7)=?", (selected_month,)).fetchone()[0]
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><title>KSP BIO CRM Dashboard</title>{{style|safe}}
    <style>
      body {background:#F4F7F5;color:#24382D}
      .container h1 {color:#123D2B;margin-bottom:20px}
      .dash-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin:18px 0 24px}
      .dash-card {border-radius:14px;padding:20px;border:1px solid rgba(20,50,35,.08);box-shadow:0 4px 12px rgba(18,61,43,.07);min-height:92px}
      .dash-card b {display:block;font-size:17px;color:#34463B}
      .dash-card h2 {font-size:31px;margin:12px 0 0;color:#172A20;overflow-wrap:anywhere}
      .c-total{background:#E1F0E6}.c-pending{background:#FFF0D9}.c-delivered{background:#DDEBFF}.c-returned{background:#FCE4E4}
      .c-products{background:#EEE5FF}.c-stock{background:#DDF5F2}.c-low{background:#FFF0DF}.c-out{background:#F1E4E7}
      .c-pending-del{background:#E5E7EB}.c-dispatched{background:#DCEBFA}.c-today-orders{background:#FDE2C5}.c-today{background:#DDF3E4}.c-overall{background:#D7F0E8}.c-month{background:#E9E2FF}
      .month-form{display:flex;align-items:end;gap:12px;flex-wrap:wrap;background:#fff;padding:16px;border-radius:12px;box-shadow:0 2px 8px #ddd}
      .month-form label{margin:0}.month-form input{max-width:190px;margin-top:6px}
      @media(max-width:600px){.dash-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.dash-card{padding:14px}.dash-card b{font-size:14px}.dash-card h2{font-size:23px}}
    </style></head><body>
    <div class="header">KSP BIO CRM</div>
    <nav><a href="/">Dashboard</a><a href="/leads">Order Sheet</a><a href="/reports">Reports / Filters</a><a href="/add-order">Add Order</a><a href="/products">Product Master</a><a href="/settings">Settings</a></nav>
    <div class="container"><h1>Dashboard</h1>
      <div class="dash-grid">
        <div class="dash-card c-total"><b>Total Orders</b><h2>{{total_orders}}</h2></div>
        <div class="dash-card c-today-orders"><b>Today's Orders</b><h2>{{today_orders}}</h2></div>
        <div class="dash-card c-pending"><b>Pending Orders</b><h2>{{pending_orders}}</h2></div>
        <div class="dash-card c-delivered"><b>Delivered Orders</b><h2>{{delivered_orders}}</h2></div>
        <div class="dash-card c-returned"><b>Returned Orders</b><h2>{{returned_orders}}</h2></div>
        <div class="dash-card c-products"><b>Total Products</b><h2>{{total_products}}</h2></div>
        <div class="dash-card c-stock"><b>Current Stock (units)</b><h2>{{'%.2f'|format(total_stock)}}</h2></div>
        <div class="dash-card c-low"><b>Low Stock (1–10)</b><h2>{{low_stock}}</h2></div>
        <div class="dash-card c-out"><b>Out of Stock</b><h2>{{out_of_stock}}</h2></div>
        <div class="dash-card c-pending-del"><b>Pending Deliveries</b><h2>{{pending_deliveries}}</h2></div>
        <div class="dash-card c-dispatched"><b>Dispatched Deliveries</b><h2>{{dispatched_deliveries}}</h2></div>
        <div class="dash-card c-today"><b>Today's Sales</b><h2>₹{{'%.2f'|format(today_sales)}}</h2></div>
        <div class="dash-card c-overall"><b>Overall Sales</b><h2>₹{{'%.2f'|format(overall_sales)}}</h2></div>
        <div class="dash-card c-month"><b>Monthly Sales ({{selected_month}})</b><h2>₹{{'%.2f'|format(month_sales)}}</h2></div>
      </div>
      <form class="month-form" method="get" action="/">
        <div><label for="month">Select month for Monthly Sales</label><input id="month" type="month" name="month" value="{{selected_month}}" required></div>
        <button type="submit">Show Sales</button>
      </form>
      <p style="margin-top:20px"><a class="btn" href="/add-order">+ Add New Order</a> <a class="btn" href="/products">Manage Products</a></p>
    </div></body></html>
    """, style=STYLE, total_orders=total_orders, today_orders=today_orders, pending_orders=pending_orders,
       delivered_orders=delivered_orders, returned_orders=returned_orders,
       total_products=total_products, total_stock=total_stock, low_stock=low_stock,
       out_of_stock=out_of_stock, pending_deliveries=pending_deliveries,
       dispatched_deliveries=dispatched_deliveries, today_sales=today_sales,
       overall_sales=overall_sales, month_sales=month_sales, selected_month=selected_month)


@app.route("/products")
def products():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products ORDER BY product_name, packing").fetchall()
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><title>Product Master</title>{{style|safe}}</head><body>
    <div class="header">KSP BIO CRM</div><nav><a href="/">Dashboard</a><a href="/reports">Reports / Filters</a><a href="/add-order">Add Order</a><a href="/products">Product Master</a><a href="/settings">Settings</a></nav>
    <div class="container"><h1>Product Master</h1><a class="btn" href="/add-product">+ Add Product</a>
    <table><tr><th>ID</th><th>Product</th><th>Packing</th><th>Stock</th></tr>
    {% for p in rows %}<tr><td>{{p.id}}</td><td>{{p.product_name}}</td><td>{{p.packing}}</td><td>{{p.stock}}</td></tr>{% else %}<tr><td colspan="4">No products yet. Add a product first.</td></tr>{% endfor %}
    </table></div></body></html>
    """, style=STYLE, rows=rows)


@app.route("/add-product")
def add_product():
    return render_template_string("""
    <!doctype html><html><head><title>Add Product</title>{{style|safe}}</head><body>
    <div class="header">KSP BIO CRM</div><nav><a href="/">Dashboard</a><a href="/products">Product Master</a><a href="/settings">Settings</a></nav>
    <div class="container"><h1>Add Product</h1><form method="post" action="/save-product">
    <label>Product Name</label><input name="product_name" required>
    <label>Packing</label><select name="packing" required><option value="">Select packing</option><option>500 ml</option><option>1 Liter</option><option>100 ml</option></select>
    <label>Opening Stock</label><input type="number" step="0.01" name="stock" value="0" min="0">
    <button type="submit">Save Product</button></form></div></body></html>
    """, style=STYLE)


@app.route("/save-product", methods=["POST"])
def save_product():
    product_name = request.form.get("product_name", "").strip()
    packing = request.form.get("packing", "").strip()
    try:
        stock = float(request.form.get("stock") or 0)
    except ValueError:
        stock = 0
    if product_name and packing:
        conn = get_db()
        exists = conn.execute(
            "SELECT id FROM products WHERE lower(product_name)=lower(?) AND packing=?",
            (product_name, packing)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO products (product_name, packing, stock) VALUES (?, ?, ?)",
                (product_name, packing, stock)
            )
            conn.commit()
        conn.close()
    return redirect("/products")


@app.route("/orders")
def orders():
    conn = get_db()
    rows = conn.execute("""
        SELECT o.*,
        COALESCE((SELECT GROUP_CONCAT(p.product_name || ' - ' || COALESCE(oi.packing,''), ', ')
                  FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id
                  WHERE oi.order_id=o.id), o.product, '') AS display_products,
        COALESCE((SELECT SUM(oi.quantity) FROM order_items oi WHERE oi.order_id=o.id), o.quantity, 0) AS display_qty
        FROM orders o ORDER BY o.id DESC
    """).fetchall()
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><title>Orders</title>{{style|safe}}</head><body>
    
<div class="header">KSP BIO CRM</div><nav><a href="/">Dashboard</a><a href="/leads">Order Sheet</a><a href="/reports">Reports / Filters</a><a href="/add-order">Add Order</a><a href="/products">Product Master</a><a href="/settings">Settings</a></nav>

    <div class="container"><h1>Orders</h1><a class="btn" href="/add-order">+ Add New Order</a> <a class="btn" href="/reports">Open Reports / Filters</a>
    <p class="small">Delivery status ani credit status pratyek order sathi ithe update kara.</p>
    <table><tr><th>Order No</th><th>Date</th><th>Customer</th><th>Products</th><th>Total</th><th>Payment</th><th>Delivery Status</th><th>Credit Received?</th><th>Actions</th></tr>
    {% for r in rows %}<tr><td>{{r.order_no}}</td><td>{{r.order_date}}</td><td>{{r.customer}}<br>{{r.phone or ''}}</td><td>{{r.display_products}}</td><td>₹{{'%.2f'|format(r.grand_total or 0)}}</td><td>{{r.payment_status}}</td>
    <td><form method="post" action="/update-order-status/{{r.id}}" style="box-shadow:none;padding:0;margin:0"><input type="hidden" name="credit_received" value="{{r.credit_received or 'No'}}"><select name="delivery_status" onchange="this.form.submit()"><option {% if (r.delivery_status or 'Pending')=='Pending' %}selected{% endif %}>Pending</option><option {% if r.delivery_status=='Dispatched' %}selected{% endif %}>Dispatched</option><option {% if r.delivery_status=='Delivered' %}selected{% endif %}>Delivered</option><option {% if r.delivery_status=='Returned' %}selected{% endif %}>Returned</option></select></form></td>
    <td><form method="post" action="/update-order-status/{{r.id}}" style="box-shadow:none;padding:0;margin:0"><input type="hidden" name="delivery_status" value="{{r.delivery_status or 'Pending'}}"><select name="credit_received" onchange="this.form.submit()"><option {% if (r.credit_received or 'No')=='No' %}selected{% endif %}>No</option><option {% if r.credit_received=='Yes' %}selected{% endif %}>Yes</option></select></form></td>
  
<td>
    <a class="btn" href="/post-details/{{r.id}}">Weight / Tracking</a>
    <a class="btn" href="/post-print/{{r.id}}" target="_blank">Print Post</a>
    <a class="btn" href="/print-bill/{{r.id}}" target="_blank">Print Bill</a>
</td></tr>
{% else %}
<tr><td colspan="9">No orders yet.</td></tr>
{% endfor %}
</table></div></body></html>
""", style=STYLE, rows=rows)


@app.route("/update-order-status/<int:order_id>", methods=["POST"])
def update_order_status(order_id):
    delivery_status = request.form.get('delivery_status', 'Pending')
    credit_received = request.form.get('credit_received', 'No')
    if delivery_status not in ['Pending', 'Dispatched', 'Delivered', 'Returned']:
        delivery_status = 'Pending'
    if credit_received not in ['Yes', 'No']:
        credit_received = 'No'
    conn = get_db()
    conn.execute('UPDATE orders SET delivery_status=?, credit_received=? WHERE id=?',
                 (delivery_status, credit_received, order_id))
    conn.commit()
    conn.close()
    return redirect('/orders')


@app.route("/reports")
def reports():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    customer = request.args.get('customer', '').strip()
    product = request.args.get('product', '').strip()
    delivery_status = request.args.get('delivery_status', '').strip()
    payment_status = request.args.get('payment_status', '').strip()
    credit_received = request.args.get('credit_received', '').strip()

    state = request.args.get('state', '').strip()
    advisor = request.args.get('advisor', '').strip()

    clauses, params = [], []
    if start_date:
        clauses.append('o.order_date >= ?'); params.append(start_date)
    if end_date:
        clauses.append('o.order_date <= ?'); params.append(end_date)
    if customer:
        clauses.append('(o.customer LIKE ? OR o.phone LIKE ?)'); params.extend([f'%{customer}%', f'%{customer}%'])
    if product:
        clauses.append('(o.product LIKE ? OR EXISTS (SELECT 1 FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id WHERE oi.order_id=o.id AND (p.product_name LIKE ? OR oi.packing LIKE ?)))')
        params.extend([f'%{product}%', f'%{product}%', f'%{product}%'])
    if delivery_status:
        clauses.append("COALESCE(o.delivery_status,'Pending') = ?"); params.append(delivery_status)
    if payment_status:
        clauses.append("COALESCE(o.payment_status,\'Pending\') = ?"); params.append(payment_status)
    if credit_received:
        clauses.append("COALESCE(o.credit_received,'No') = ?"); params.append(credit_received)

    if state:
        clauses.append("COALESCE(o.state,'') = ?")
        params.append(state)
    if advisor:
        clauses.append("COALESCE(o.advisor,'') = ?")
        params.append(advisor)

    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    conn = get_db()
    rows = conn.execute(f"""SELECT o.*,
        COALESCE((SELECT GROUP_CONCAT(p.product_name || ' - ' || COALESCE(oi.packing,''), ', ') FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id WHERE oi.order_id=o.id), o.product, '') AS display_products,
        COALESCE((SELECT SUM(oi.quantity) FROM order_items oi WHERE oi.order_id=o.id), o.quantity, 0) AS display_qty
        FROM orders o {where} ORDER BY o.id DESC""", params).fetchall()
    total_amount = sum((r['grand_total'] or 0) for r in rows)

    states = [r[0] for r in conn.execute(
        "SELECT DISTINCT state FROM orders WHERE state IS NOT NULL AND TRIM(state) != '' ORDER BY state"
    ).fetchall()]

    advisors = [
        "Sangram D",
        "Prachi",
        "Akshay",
        "Ashwini",
        "Shweta H",
        "Yogesh",
        "Aditya M",
        "Komal p",
        "Vaishnavi J",
        "Rohit G",
        "Bhagwat G",
        "Radhika K"
    ]

    conn.close()   
    return render_template_string("""
    <!doctype html><html><head><title>Reports / Filters</title>{{style|safe}}</head><body>
    <div class="header">KSP BIO CRM</div><nav><a href="/">Dashboard</a><a href="/reports">Reports / Filters</a><a href="/add-order">Add Order</a><a href="/products">Product Master</a><a href="/settings">Settings</a></nav>
    <div class="container"><h1>Reports / Filters</h1>
    <form method="get"><div class="grid">
      <div><label>From Date</label><input type="date" name="start_date" value="{{start_date}}"></div>
      <div><label>To Date</label><input type="date" name="end_date" value="{{end_date}}"></div>

      <div><label>State</label><select name="state">
        <option value="">All States</option>
        {% for x in states %}
        <option value="{{x}}" {% if state==x %}selected{% endif %}>{{x}}</option>
        {% endfor %}
      </select></div>

      <div><label>Advisor</label><select name="advisor">
        <option value="">All Advisors</option>
        {% for x in advisors %}
        <option value="{{x}}" {% if advisor==x %}selected{% endif %}>{{x}}</option>
        {% endfor %}
      </select></div>

      <div><label>Customer Name / Mobile</label><input name="customer" value="{{customer}}" placeholder="Name or mobile"></div>


      <div><label>Customer Name / Mobile</label><input name="customer" value="{{customer}}" placeholder="Name or mobile"></div>
      <div><label>Product Name</label><input name="product" value="{{product}}" placeholder="Product"></div>
      <div><label>Delivery Status</label><select name="delivery_status"><option value="">All</option>{% for x in ['Pending','Dispatched','Delivered','Returned'] %}<option value="{{x}}" {% if delivery_status==x %}selected{% endif %}>{{x}}</option>{% endfor %}</select></div>
      <div><label>Payment Status</label><select name="payment_status"><option value="">All</option>{% for x in ['Pending','Paid','Partial','COD'] %}<option value="{{x}}" {% if payment_status==x %}selected{% endif %}>{{x}}</option>{% endfor %}</select></div>
      <div><label>Credit Received?</label><select name="credit_received"><option value="">All</option><option {% if credit_received=='Yes' %}selected{% endif %}>Yes</option><option {% if credit_received=='No' %}selected{% endif %}>No</option></select></div>
    </div><button type="submit">Apply Filters</button> <a class="btn" href="/reports">Clear</a></form>
    <div class="card"><b>Orders found:</b> {{rows|length}} &nbsp; <b>Total order value:</b> ₹{{'%.2f'|format(total_amount)}}</div>
    <table><tr><th>Order No</th><th>Date</th><th>Customer</th><th>Mobile</th><th>Products</th><th>Total</th><th>Payment</th><th>Delivery</th><th>Credit Received</th></tr>
    {% for r in rows %}<tr><td>{{r.order_no}}</td><td>{{r.order_date}}</td><td>{{r.customer}}</td><td>{{r.phone or ''}}</td><td>{{r.display_products}}</td><td>₹{{'%.2f'|format(r.grand_total or 0)}}</td><td>{{r.payment_status}}</td><td>{{r.delivery_status or 'Pending'}}</td><td>{{r.credit_received or 'No'}}</td></tr>{% else %}<tr><td colspan="9">No matching orders.</td></tr>{% endfor %}</table>
    </div></body></html>
    """, style=STYLE, rows=rows, total_amount=total_amount, start_date=start_date, end_date=end_date,
       
        customer=customer, product=product, delivery_status=delivery_status,
        payment_status=payment_status, credit_received=credit_received,
        state=state, advisor=advisor, states=states, advisors=advisors)


@app.route("/post-details/<int:order_id>", methods=["GET", "POST"])
def post_details(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return "Order not found", 404
    if request.method == "POST":
        conn.execute("UPDATE orders SET tracking_id=?, weight=?, post_status=? WHERE id=?",
                     (request.form.get("tracking_id", "").strip(),
                      request.form.get("weight", "").strip(),
                      request.form.get("post_status", "Pending"), order_id))
        conn.commit()
        conn.close()
        return redirect(f"/post-print/{order_id}")
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><title>Post Details</title>{{style|safe}}</head><body>
    <div class="header">KSP BIO CRM</div><nav><a href="/">Dashboard</a></nav>
    <div class="container"><h1>Post Details — {{o.order_no}}</h1>
    <div class="card"><b>Customer:</b> {{o.customer}}<br><b>Address:</b> {{o.address or ''}}<br>{{o.state or ''}} {{o.pincode or ''}}<br><b>Phone:</b> {{o.phone or ''}}</div>
    <form method="post"><label>Weight (grams)</label><input name="weight" value="{{o.weight or ''}}" placeholder="Enter parcel weight" required>
    <label>Tracking ID</label><input name="tracking_id" value="{{o.tracking_id or ''}}" placeholder="Enter tracking ID">
    <label>Post Status</label><select name="post_status"><option {% if o.post_status=='Pending' %}selected{% endif %}>Pending</option><option {% if o.post_status=='Sent' %}selected{% endif %}>Sent</option></select>
    <button type="submit">Save & Create Post Format</button></form></div></body></html>
    """, style=STYLE, o=order)


@app.route("/post-print/<int:order_id>")
def post_print(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return "Order not found", 404
    items = conn.execute("""SELECT COALESCE(p.product_name, 'Product') AS product_name,
                           COALESCE(oi.packing, '') AS packing, oi.quantity
                           FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id
                           WHERE oi.order_id=?""", (order_id,)).fetchall()
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><title>Post Format {{o.order_no}}</title>{{style|safe}}
    <style>.labelbox{border:2px solid #111;padding:24px;max-width:700px;margin:24px auto;background:white;color:#111}.labelbox h2{text-align:center}.printbtn{display:block;margin:20px auto}@media print{nav,.printbtn{display:none}body{background:white}.labelbox{box-shadow:none;margin:0;max-width:none}}</style>
    </head><body><div class="header">KSP BIO CRM — Post Format</div><nav><a href="/orders">Back to Orders</a></nav>
    <div class="labelbox"><h2>PARCEL / POST DETAILS</h2><hr>
    <p><b>Order No:</b> {{o.order_no}} &nbsp; <b>Date:</b> {{o.order_date}}</p>
    <h3>TO (RECIPIENT)</h3><p><b>{{o.customer}}</b><br>{{o.address or ''}}<br>{{o.state or ''}}{% if o.pincode %} - PIN: {{o.pincode}}{% endif %}<br>Phone: {{o.phone or ''}}</p>
    <p><b>Products:</b><br>{% for item in items %}{{item.product_name}} - {{item.packing}} (Qty: {{item.quantity}})<br>{% endfor %}</p>
    <hr><p><b>Parcel Weight:</b> {{o.weight or 'NOT ENTERED'}} grams</p><p><b>Tracking ID:</b> {{o.tracking_id or 'NOT ENTERED'}}</p><p><b>Post Status:</b> {{o.post_status or 'Pending'}}</p>
    </div><button class="printbtn" onclick="window.print()">Print / Save as PDF</button></body></html>
    """, style=STYLE, o=order, items=items)


@app.route("/add-order")
def add_order():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    products = conn.execute("SELECT * FROM products ORDER BY product_name, packing").fetchall()
    conn.close()
    product_options = "".join(
        f'<option value="{p["id"]}">{p["product_name"]} - {p["packing"]}</option>'
        for p in products
    )
    return render_template_string("""
    <!doctype html><html><head><title>Add Order - KSP BIO CRM</title>{{style|safe}}
    </head><body><div class="header">KSP BIO CRM</div>
    <nav><a href="/">Dashboard</a><a href="/reports">Reports / Filters</a><a href="/add-order">Add Order</a><a href="/products">Product Master</a><a href="/settings">Settings</a></nav>
    <div class="container"><h1>Add Order</h1>
    {% if not product_options %}<div class="card">First add products in <a href="/add-product">Product Master</a>, then create an order.</div>{% endif %}
    <form method="post" action="/save-order" id="orderForm">
      <div class="section">Order Details</div><div class="grid">
        <div><label>Order Date</label><input type="date" name="order_date" value="{{today}}" required></div>
        <div><label>Customer Name</label><input name="customer" required></div>
        <div><label>Phone</label><input name="phone"></div>
        <div><label>State</label><input name="state"></div>
        <div><label>Pincode</label><input name="pincode"></div>
 
<div>
  <label>Advisor</label>
  <select name="advisor" required style="font-weight:bold;">
    <option value="">Select Advisor</option>
    <option value="Sangram D" style="background:#d9f2c2;color:#16803c;">Sangram D</option>
    <option value="Prachi" style="background:#c90000;color:white;">Prachi</option>
    <option value="Akshay" style="background:#ffc5c5;color:#b00000;">Akshay</option>
    <option value="Ashwini" style="background:#b9e0ff;color:#005bbb;">Ashwini</option>
    <option value="Shweta H" style="background:#633399;color:white;">Shweta H</option>
    <option value="Yogesh" style="background:#087b48;color:white;">Yogesh</option>
    <option value="Aditya M" style="background:#c600b8;color:white;">Aditya M</option>
    <option value="Komal p" style="background:#59421c;color:#ffe7a0;">Komal p</option>
    <option value="Vaishnavi J" style="background:#d50050;color:white;">Vaishnavi J</option>
    <option value="Rohit G" style="background:#6680d8;color:black;">Rohit G</option>
    <option value="Bhagwat G" style="background:#8aefc5;color:black;">Bhagwat G</option>
    <option value="Radhika K" style="background:#f5b6d2;color:#8b174b;">Radhika K</option>
  </select>
</div>


      </div>
      <label>Address</label><input name="address">
      <div class="section">Products in this Order</div>
      <div id="productRows"></div>
      <button type="button" id="addRow">+ Add Product</button>
      <p class="small">Select product/packing from dropdown. Add as many product rows as needed.</p>
      <div class="grid">
        <div><label>Discount (₹)</label><input type="number" step="0.01" min="0" name="discount" id="discount" value="0"></div>
        <div><label>GST (₹)</label><input type="number" step="0.01" min="0" name="gst" id="gst" value="0"></div>
        <div><label>Grand Total (₹)</label><input type="number" step="0.01" name="grand_total" id="grand_total" value="0" readonly></div>
        <div><label>Payment Status</label><select name="payment_status"><option>Pending</option><option>Paid</option><option>Partial</option><option>COD</option></select></div>
      </div>
      <div class="section">Post Details</div><div class="grid">
        <div><label>Tracking ID</label><input name="tracking_id" placeholder="Blank initially"></div>
        <div><label>Weight</label><input name="weight" placeholder="Blank initially"></div>
        <div><label>Post Status</label><select name="post_status"><option>Pending</option><option>Sent</option></select></div>
        <div><label>Invoice No</label><input name="invoice_no"></div>
      </div>
      <button type="submit" {% if not product_options %}disabled{% endif %}>Save Order</button>
    </form></div>
    <script>
    const options = {{ product_options|tojson }};
    const rows = document.getElementById('productRows');
    function addProductRow() {
      const row = document.createElement('div');
      row.className = 'product-row card';
      row.innerHTML = `
        <div><label>Product / Packing</label><select name="product_id[]" required>
          <option value="">Select Product</option>${options}</select></div>
        <div><label>Qty</label><input name="quantity[]" type="number" min="0.01" step="0.01" value="1" required></div>
        <div><label>Rate (₹)</label><input name="rate[]" type="number" min="0" step="0.01" value="0" required></div>
        <div><label>Amount (₹)</label><input class="amount" type="number" value="0" readonly></div>
        <div><button type="button" class="remove">Remove</button></div>`;
      rows.appendChild(row);
      row.querySelectorAll('input[name="quantity[]"],input[name="rate[]"]').forEach(el => el.addEventListener('input', recalculate));
      row.querySelector('.remove').addEventListener('click', () => {
        if (rows.children.length > 1) { row.remove(); recalculate(); }
        else { alert('At least one product is required.'); }
      });
      recalculate();
    }
    function recalculate() {
      let subtotal = 0;
      rows.querySelectorAll('.product-row').forEach(row => {
        const q = parseFloat(row.querySelector('[name="quantity[]"]').value) || 0;
        const r = parseFloat(row.querySelector('[name="rate[]"]').value) || 0;
        const amount = q * r;
        row.querySelector('.amount').value = amount.toFixed(2);
        subtotal += amount;
      });
      const discount = parseFloat(document.getElementById('discount').value) || 0;
      const gst = parseFloat(document.getElementById('gst').value) || 0;
      document.getElementById('grand_total').value = Math.max(0, subtotal - discount + gst).toFixed(2);
    }
    document.getElementById('addRow').addEventListener('click', addProductRow);
    document.getElementById('discount').addEventListener('input', recalculate);
    document.getElementById('gst').addEventListener('input', recalculate);
    addProductRow();
    </script></body></html>
    """, style=STYLE, today=today, product_options=product_options)


@app.route("/save-order", methods=["POST"])
def save_order():
    conn = get_db()
    try:
        product_ids = request.form.getlist("product_id[]")
        quantities = request.form.getlist("quantity[]")
        rates = request.form.getlist("rate[]")

        items = []
        subtotal = 0.0
        for i, raw_id in enumerate(product_ids):
            if not raw_id:
                continue
            product_id = int(raw_id)
            quantity = float(quantities[i] or 0)
            rate = float(rates[i] or 0)
            if quantity <= 0:
                continue
            product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                continue
            amount = quantity * rate
            subtotal += amount
            items.append((product_id, product["packing"], quantity, rate, amount, product["product_name"]))

        if not items:
            conn.close()
            return "Please add at least one valid product to this order.", 400

        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        order_no = request.form.get("order_no") or "KSP-" + str(count + 1).zfill(5)
        discount = float(request.form.get("discount") or 0)
        gst = float(request.form.get("gst") or 0)
        grand_total = max(0, subtotal - discount + gst)
        total_qty = sum(item[2] for item in items)
        first_product_label = items[0][5] + " - " + items[0][1]
        first_rate = items[0][3]

        cur = conn.execute("""
            INSERT INTO orders (
                order_no, order_date, customer, phone, address, state, pincode,
                advisor, product, quantity, rate, subtotal, discount, gst,
                grand_total, payment_status, tracking_id, weight, post_status, invoice_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_no, request.form.get("order_date"), request.form.get("customer"),
            request.form.get("phone"), request.form.get("address"), request.form.get("state"),
            request.form.get("pincode"), request.form.get("advisor"), first_product_label,
            total_qty, first_rate, subtotal, discount, gst, grand_total,
            request.form.get("payment_status"), request.form.get("tracking_id"),
            request.form.get("weight"), request.form.get("post_status"), request.form.get("invoice_no")
        ))
        order_id = cur.lastrowid

        for product_id, packing, quantity, rate, amount, _name in items:
            # Check stock availability before reducing stock
            product = conn.execute(
                "SELECT stock FROM products WHERE id = ?",
                (product_id,)
            ).fetchone()

            if product is None or product["stock"] < quantity:
                raise ValueError(
                    f"Insufficient stock for product ID {product_id}"
                )

            conn.execute("""
                INSERT INTO order_items
                (order_id, product_id, packing, quantity, rate, amount)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (order_id, product_id, packing, quantity, rate, amount))

            conn.execute("""
                UPDATE products
                SET stock = stock - ?
                WHERE id = ?
            """, (quantity, product_id))

        conn.execute("""
            INSERT INTO leads (
                order_id, date, farmer_name, mob_no, add1, pin, product,
                total_amount, advisor_name, tracking_id, confirmation, order_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id,
            request.form.get("order_date", ""),
            request.form.get("customer", ""),
            request.form.get("phone", ""),
            request.form.get("address", ""),
            request.form.get("pincode", ""),
            first_product_label,
            grand_total,
            request.form.get("advisor", ""),
            request.form.get("tracking_id", ""),
            "Pending",
            request.form.get("post_status") or "Pending"
        ))


        conn.commit()
    except Exception as exc:
        conn.rollback()
        return f"Could not save order: {exc}", 500
    finally:
        conn.close()
    return redirect("/leads")

      

@app.route("/print-bill/<int:order_id>")
def print_bill(order_id):
    conn = get_db()

    order = conn.execute(
        "SELECT * FROM orders WHERE id = ?", (order_id,)
    ).fetchone()

    if not order:
        conn.close()
        return "Order not found", 404

    items = conn.execute("""
        SELECT p.product_name, COALESCE(oi.packing, '') AS packing,
               oi.quantity, oi.rate, oi.amount
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
    """, (order_id,)).fetchall()

    conn.close()

    if not items:
        items = [{
            "product_name": order["product"] or order["products"] or "",
            "packing": "",
            "quantity": order["quantity"] or 0,
            "rate": order["rate"] or 0,
            "amount": order["subtotal"] or 0
        }]

    return render_template_string("""
    <!doctype html>
    <html>
    <head>
        <title>KSP BIO - Bill</title>
        <style>
            body { font-family: Arial; margin: 30px; color: #222; }
            .bill { max-width: 800px; margin: auto; border: 1px solid #ccc; padding: 25px; }
            .center { text-align: center; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th, td { border: 1px solid #ccc; padding: 9px; text-align: left; }
            .total { text-align: right; margin-top: 20px; }
            .print { padding: 10px 18px; cursor: pointer; }
            @media print {
                .print { display: none; }
                body { margin: 0; }
                .bill { border: none; }
            }
        </style>
    </head>
   <body>
    <div class="bill">
        <div class="center">
            <h1>KSP BIO</h1>
            <p>Pune, Maharashtra 411048</p>
            <p>Phone: 8793960084</p>
            <p>GSTIN: 27ABFFK6340K1Z3</p>
            <h2>BILL</h2>
        </div>
        <hr>
        <p><b>Order No:</b> {{ order["order_no"] or order["id"] }}</p>
        <p><b>Date:</b> {{ order["order_date"] or "" }}</p>      
        <p><b>Customer:</b> {{ order["customer"] or "" }}</p>
        <p><b>Mobile:</b> {{ order["phone"] or "" }}</p>
        <p><b>City:</b> Pune - {{ order["pincode"] or "" }}</p>

        <table>
            <tr>
                <th>Product</th>
                <th>Qty</th>
                <th>Rate (₹)</th>
                <th>Amount (₹)</th>
            </tr>
            {% for item in items %}
            <tr>
                <td>{{ item["product_name"] or "" }} {{ item["packing"] or "" }}</td>
                <td>{{ item["quantity"] or 0 }}</td>
                <td>{{ "%.2f"|format(item["rate"] or 0) }}</td>
                <td>{{ "%.2f"|format(item["amount"] or 0) }}</td>
            </tr>
            {% endfor %}
        </table>

        <div class="total">
            <p>Subtotal: ₹{{ "%.2f"|format(order["subtotal"] or 0) }}</p>
            <p>Discount: ₹{{ "%.2f"|format(order["discount"] or 0) }}</p>
            <h2>Final Total: ₹{{ "%.2f"|format(order["grand_total"] or 0) }}</h2>
        </div>

        <p class="center">Thank you for your business!</p>
        <div class="center">
            <button class="print" onclick="window.print()">Print Bill / Save PDF</button>
        </div>
    </div>
    </body>
    </html>
    """, order=order, items=items)
@app.route("/leads/bulk", methods=["POST"])
def leads_bulk():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, farmer_name TEXT, mob_no TEXT,
            alternate TEXT, add1 TEXT, add2 TEXT, pin TEXT,
            product TEXT, total_amount TEXT, advance TEXT,
            lable TEXT, advisor_name TEXT, tracking_id TEXT,
            confirmation TEXT, remarks TEXT, status TEXT,
            delivery_comment TEXT, order_status TEXT,
            call_date TEXT, credited TEXT
        )
    """)

    data = request.form.get("bulk_data", "").strip()

    if data:
        for line in data.splitlines():
            cells = line.split("\t")
            cells = (cells + [""] * 20)[:20]

            conn.execute("""
                INSERT INTO leads (
                    date, farmer_name, mob_no, alternate, add1, add2,
                    pin, product, total_amount, advance, lable,
                    advisor_name, tracking_id, confirmation, remarks,
                    status, delivery_comment, order_status, call_date, credited
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, cells)

    conn.commit()
    conn.close()
    return redirect("/leads")
@app.route("/leads/delete/<int:lead_id>", methods=["POST"])
def delete_lead(lead_id):
    conn = get_db()
    lead = conn.execute("SELECT order_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead and lead["order_id"] is not None:
        conn.execute("INSERT OR IGNORE INTO deleted_order_sheet_links(order_id) VALUES (?)", (lead["order_id"],))
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()
    return "", 200
@app.route("/leads", methods=["GET", "POST"])
def leads():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            farmer_name TEXT,
            mob_no TEXT,
            alternate TEXT,
            add1 TEXT,
            add2 TEXT,
            pin TEXT,
            product TEXT,
            total_amount TEXT,
            advance TEXT,
            lable TEXT,
            advisor_name TEXT,
            tracking_id TEXT,
            confirmation TEXT,
            remarks TEXT,
            status TEXT,
            delivery_comment TEXT,
            order_status TEXT,
            call_date TEXT,
            credited TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_edit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT
        )
    """)

    columns = [
        ("date", "DATE"),
        ("farmer_name", "FARMER NAME"),
        ("mob_no", "Mob No"),
        ("alternate", "Alternate"),
        ("add1", "ADD1-AT/POST/Villege"),
        ("add2", "ADD2-TAL/DIST"),
        ("pin", "PIN"),
        ("product", "PRODUCT"),
        ("total_amount", "Total Amount"),
        ("advance", "ADVANCE"),
        ("lable", "LABLE"),
        ("advisor_name", "ADVISOR NAME"),
        ("tracking_id", "Tracking ID"),
        ("confirmation", "Confirmation"),
        ("remarks", "Add Remarks For All"),
        ("status", "Status"),
        ("delivery_comment", "Add Delivery Call Comment Here"),
        ("order_status", "Order Status"),
        ("call_date", "Call Date"),
        ("credited", "Credited")
    ]

    if request.method == "POST":
        action = request.form.get("action", "add")

        if action == "add":
            values = [request.form.get(key, "") for key, label in columns]

            conn.execute("""
                INSERT INTO leads (
                    date, farmer_name, mob_no, alternate, add1, add2,
                    pin, product, total_amount, advance, lable,
                    advisor_name, tracking_id, confirmation, remarks,
                    status, delivery_comment, order_status, call_date, credited
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, values)

        elif action == "update":
            lead_id = request.form.get("lead_id")
            values = [request.form.get(key, "") for key, label in columns]
            old_row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            if old_row:
                # Individual staff login is not configured yet, so record a clear placeholder.
                changed_by = session.get("username", "Unknown user")
                changed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for (key, label), new_value in zip(columns, values):
                    old_value = "" if old_row[key] is None else str(old_row[key])
                    new_value_text = "" if new_value is None else str(new_value)
                    if old_value != new_value_text:
                        conn.execute("""
                            INSERT INTO lead_edit_history
                            (lead_id, changed_at, changed_by, field_name, old_value, new_value)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (lead_id, changed_at, changed_by, label, old_value, new_value_text))
            values.append(lead_id)

            conn.execute("""
                UPDATE leads SET
                    date=?, farmer_name=?, mob_no=?, alternate=?,
                    add1=?, add2=?, pin=?, product=?, total_amount=?,
                    advance=?, lable=?, advisor_name=?, tracking_id=?,
                    confirmation=?, remarks=?, status=?, delivery_comment=?,
                    order_status=?, call_date=?, credited=?
                WHERE id=?
            """, values)
        conn.commit()
        conn.close()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": True, "lead_id": request.form.get("lead_id", "")}, 200
        return redirect("/leads")

    confirmation_filter = request.args.get("confirmation", "").strip()
    advisor_filter = request.args.get("advisor_name", "").strip()
    amount_summary = request.args.get("amount_summary", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    selected_month = request.args.get("month", "").strip()
    if selected_month:
        try:
            datetime.strptime(selected_month, "%Y-%m")
        except ValueError:
            selected_month = ""
    conditions = []
    params = []
    if confirmation_filter:
        conditions.append("TRIM(COALESCE(confirmation,'')) = ?")
        params.append(confirmation_filter)
    if advisor_filter:
        conditions.append("TRIM(COALESCE(advisor_name,'')) = ?")
        params.append(advisor_filter)
    # Normalize common saved date formats (YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY)
    normalized_lead_date = "CASE \
        WHEN date(date) IS NOT NULL THEN date(date) \
        WHEN date GLOB '[0-3][0-9]-[0-1][0-9]-[0-9][0-9][0-9][0-9]' THEN substr(date,7,4)||'-'||substr(date,4,2)||'-'||substr(date,1,2) \
        WHEN date GLOB '[0-3][0-9]/[0-1][0-9]/[0-9][0-9][0-9][0-9]' THEN substr(date,7,4)||'-'||substr(date,4,2)||'-'||substr(date,1,2) \
        ELSE NULL END"
    if selected_month:
        conditions.append(f"substr(({normalized_lead_date}),1,7) = ?")
        params.append(selected_month)
    if from_date:
        conditions.append(f"({normalized_lead_date}) >= date(?)")
        params.append(from_date)
    if to_date:
        conditions.append(f"({normalized_lead_date}) <= date(?)")
        params.append(to_date)
    sql = "SELECT * FROM leads"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, params).fetchall()
    advisor_options = conn.execute("SELECT DISTINCT TRIM(advisor_name) AS advisor_name FROM leads WHERE TRIM(COALESCE(advisor_name,'')) <> '' ORDER BY advisor_name COLLATE NOCASE").fetchall()
    total_sale = 0.0
    for row in rows:
        raw_amount = str(row["total_amount"] or "").replace(",", "").replace("₹", "").strip()
        try:
            total_sale += float(raw_amount)
        except (TypeError, ValueError):
            pass

    conn.close()    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Order Sheet</title>
        <style>
            * { box-sizing: border-box; }
            body { font-family: Arial, sans-serif; background:#F7FAF8; color:#24382D; margin:0; padding:24px 28px 24px 286px; font-size:19px; line-height:1.6; }
            .crm-sidebar { position:fixed; left:0; top:0; bottom:0; width:260px; background:#176B45; padding:22px 13px; z-index:1000; overflow-y:auto; box-shadow:5px 0 20px rgba(15,40,29,.08); }
            .crm-sidebar .brand { color:#fff; font-size:21px; font-weight:800; padding:4px 12px 26px; }
            .crm-sidebar .brand small { display:block; font-size:10px; letter-spacing:1.5px; color:#b9d7c7; margin-top:5px; }
            .crm-sidebar a { display:block; color:#e4f0e9; text-decoration:none; padding:13px 14px; margin:5px 0; border-radius:9px; font-size:16px; font-weight:600; }
            .crm-sidebar a:hover,.crm-sidebar a.active { background:#235a40; color:#fff; }
            h2 { color:#176B45; font-size:34px; margin-top:0; }
            h3 { font-size:25px; }
            .table-wrap { overflow:auto; overflow-x:scroll; overflow-y:auto; max-height:72vh; background:white; padding:12px; border-radius:10px; box-shadow:0 4px 14px rgba(18,61,43,.07); scrollbar-width:auto; scrollbar-color:#176B45 #dce7df; }
            .table-wrap::-webkit-scrollbar { height:18px; width:18px; }
            .table-wrap::-webkit-scrollbar-track { background:#dce7df; border-radius:9px; }
            .table-wrap::-webkit-scrollbar-thumb { background:#176B45; border-radius:9px; border:3px solid #dce7df; }
            .table-wrap tr.today-row td { background:#DDF3E4 !important; }
            .table-wrap tr.old-row td { background:#FFF1B8 !important; }
            .table-wrap tr.today-row:hover td { background:#c8ebd2 !important; }
            .table-wrap tr.old-row:hover td { background:#ffe89a !important; }
            .month-filter { display:flex; flex-wrap:wrap; align-items:end; gap:12px; padding:14px; background:#fff; border:1px solid #dce7df; border-radius:10px; margin:12px 0; }
            .month-filter label { font-size:18px; font-weight:bold; }
            .month-filter input { font-size:18px; padding:12px; }
            .save-status { font-weight:bold; color:#176B45; margin-left:12px; }
            @media(max-width:800px) { body { padding:14px; } .crm-sidebar { position:relative; width:auto; bottom:auto; display:flex; flex-wrap:wrap; gap:4px; margin:-14px -14px 20px; } .crm-sidebar .brand { width:100%; padding-bottom:8px; } .crm-sidebar a { padding:9px; } }
            table {
                border-collapse: collapse;
                min-width: 2200px;
                width: 100%;
            }
            th, td {
                  border-bottom: 1px solid #ddd;
                  border-right: 1px solid #ddd;
                   padding: 11px;
                   font-size: 18px;
                   white-space: nowrap;
                   width: auto;
            }
            th {
                background: #176B45;
                color: white;
                position: sticky;
                top: 0;
            }
            tfoot { position: sticky; bottom: 0; z-index: 3; }
            tfoot td { position: sticky; bottom: 0; }
            input, select {
                width: auto;
                padding: 10px;
                font-size: 17px;
                box-sizing: border-box;
            }
            button {
                background: #176B45;
                color: white;
                padding: 8px 12px;
                border: none;
                cursor: pointer;
                border-radius: 4px;
            }
            .add-form {
                background: white;
                padding: 15px;
                margin-bottom: 20px;
                border-radius: 8px;
            }
            .field {
                display: inline-block;
                margin: 5px;
            }
            label {
                display: block;
                font-size: 16px;
                margin-bottom: 6px;
            }
        </style>
<script>
function autoFitInputs() {
    document.querySelectorAll('.table-wrap input:not([type="hidden"])').forEach(input => {
        const text = input.value || input.placeholder || "";
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        ctx.font = getComputedStyle(input).font;
        const textWidth = ctx.measureText(text).width;
        input.style.width = Math.max(100, textWidth + 20) + "px";
    });
}

window.addEventListener("load", autoFitInputs);
</script>
    </head>
    <body>
        <aside class="crm-sidebar"><div class="brand">KSP BIO CRM<small>BUSINESS MANAGEMENT</small></div>
          <a href="/">Dashboard</a><a href="/leads" class="active">Order Sheet</a><a href="/reports">Reports / Filters</a><a href="/add-order">Add Order</a><a href="/products">Product Master</a><a href="/settings">Settings</a>
        </aside>
        <h2>Order Sheet</h2>
        <p>Total Leads: {{ rows|length }} <span class="save-status" id="saveStatus">Changes save automatically</span></p>
       <h3>Bulk Paste from Google Sheets</h3>

<form method="POST" action="/leads/bulk">
    <textarea name="bulk_data" rows="8"
        style="width:100%; box-sizing:border-box;"
        placeholder="Google Sheets madhun data ithe paste kar..."></textarea>
    <br><br>
    <button type="submit">Save All Rows</button>
</form>
<hr>
      
        <h3>Saved Leads — Edit and Save</h3>
<form method="GET" action="/leads" class="month-filter">
    <div><label for="month">Filter by Month</label><input type="month" name="month" id="month" value="{{ request.args.get('month','') }}"></div>
    <div><label for="from_date">From Date</label><input type="date" name="from_date" id="from_date" value="{{ request.args.get('from_date','') }}"></div>
    <div><label for="to_date">To Date</label><input type="date" name="to_date" id="to_date" value="{{ request.args.get('to_date','') }}"></div>
    <div><label for="advisor_name_filter">Advisor Name</label><select name="advisor_name" id="advisor_name_filter" style="min-width:180px;">
        <option value="" {% if not request.args.get('advisor_name','') %}selected{% endif %}>All Advisors</option>
        {% for advisor in advisor_options %}
        <option value="{{ advisor['advisor_name'] }}" {% if request.args.get('advisor_name','') == advisor['advisor_name'] %}selected{% endif %}>{{ advisor['advisor_name'] }}</option>
        {% endfor %}
    </select></div>
    <div><label for="amount_summary">Amount</label><select name="amount_summary" id="amount_summary" style="min-width:160px;">
        <option value="" {% if not request.args.get('amount_summary','') %}selected{% endif %}>Hide Total</option>
        <option value="total" {% if request.args.get('amount_summary','') == 'total' %}selected{% endif %}>Show Total Sale</option>
    </select></div>
    <div><label for="confirmation_filter">Confirmation</label><select name="confirmation" id="confirmation_filter" style="min-width:140px;">
        <option value="" {% if not request.args.get('confirmation','') %}selected{% endif %}>All</option>
        <option value="Confirm" {% if request.args.get('confirmation','') == 'Confirm' %}selected{% endif %}>Confirm</option>
        <option value="Not Confirm" {% if request.args.get('confirmation','') == 'Not Confirm' %}selected{% endif %}>Not Confirm</option>
        <option value="Pending" {% if request.args.get('confirmation','') == 'Pending' %}selected{% endif %}>Pending</option>
    </select></div>
    <button type="submit">Apply Filters</button><a class="btn" href="/leads">Clear Filters</a>
    <a class="btn" href="/post-export?from_date={{ request.args.get('from_date','') }}&to_date={{ request.args.get('to_date','') }}">Post Export</a>
    <a class="btn" href="/edit-history">Edit History</a>
</form>
<button type="button" onclick="deleteSelected()">Delete Selected Rows</button>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
<th><input type="checkbox" id="selectAll"></th>
                        <th>Sr. No.</th>
                        {% for key, label in columns %}
                        <th>
                            {{ label }}

                        </th>
                        {% endfor %}
                    </tr>
                </thead>

                <tbody>
                    {% for lead in rows %}
                    <tr class="{% set rowdate = (lead['date'] or '') %}{% if rowdate == today_iso or rowdate == today_dmy or rowdate == today_slash %}today-row{% else %}old-row{% endif %}">
                       <td><input type="checkbox" class="rowSelect" value="{{ lead['id'] }}"></td>
                        <form method="POST">
                            <input type="hidden" name="action" value="update">
                            <input type="hidden" name="lead_id"
                                   value="{{ lead['id'] }}">
                            <td>{{ lead['id'] }}</td>

                            {% for key, label in columns %}
                            <td>
                                {% if key == "status" %}
                                <select name="{{ key }}">
                                    <option value="">Select</option>
                                    <option value="Delivered"
                                    {% if lead[key] == "Delivered" %}selected{% endif %}>
                                        Delivered
                                    </option>
                                    <option value="Return"
                                    {% if lead[key] == "Return" %}selected{% endif %}>
                                        Return
                                    </option>
                                </select>

                                {% elif key == "confirmation" %}
                                <select name="{{ key }}">
                                    <option value="">Select</option>
                                    <option value="Confirm" {% if lead[key] == "Confirm" %}selected{% endif %}>Confirm</option>
                                    <option value="Not Confirm" {% if lead[key] == "Not Confirm" %}selected{% endif %}>Not Confirm</option>
                                    <option value="Pending" {% if lead[key] == "Pending" %}selected{% endif %}>Pending</option>
                                </select>

                                {% elif key == "credited" %}
                                <select name="{{ key }}">
                                    <option value="">Select</option>
                                    <option value="Yes"
                                    {% if lead[key] == "Yes" %}selected{% endif %}>
                                        Yes
                                    </option>
                                    <option value="No"
                                    {% if lead[key] == "No" %}selected{% endif %}>
                                        No
                                    </option>
                                </select>

                                {% elif key == "date" or key == "call_date" %}
                                <input type="date" name="{{ key }}"
                                       value="{{ lead[key] or '' }}">

                                {% else %}
                               <input name="{{ key }}"
       value="{{ lead[key] or '' }}"
       style="border:none; outline:none; background:transparent;
              box-shadow:none; padding:4px 2px;width:max-content; min-width:100px;">
                                {% endif %}
                            </td>
                            {% endfor %}

                        </form>
                    </tr>
                    {% endfor %}
                </tbody>
                {% if amount_summary == 'total' %}
                <tfoot>
                    <tr style="background:#176B45;color:#fff;font-weight:bold;">
                        <td colspan="10" style="text-align:right;color:#fff;background:#176B45;">
                            {% if advisor_filter %}{{ advisor_filter }} — {% endif %}TOTAL SALE
                        </td>
                        <td style="color:#fff;background:#176B45;font-size:20px;">₹{{ '%.2f'|format(total_sale) }}</td>
                        {% for key, label in columns[9:] %}<td style="background:#176B45;"></td>{% endfor %}
                    </tr>
                </tfoot>
                {% endif %}
            </table>
        </div>

<script>
// Reliable auto-save: serialize controls directly from each table row.
// HTML forms inside table rows are invalid in browsers, so do not rely on FormData(form).
const saveTimers = new Map();
const savingRows = new Set();
function rowPayload(row) {
    const payload = new FormData();
    payload.append('action', 'update');
    const id = row.querySelector('.rowSelect')?.value;
    if (!id) return null;
    payload.append('lead_id', id);
    row.querySelectorAll('input[name], select[name], textarea[name]').forEach(field => {
        if (field.name !== 'lead_id' && field.name !== 'action') payload.append(field.name, field.value ?? '');
    });
    return {payload, id};
}
async function saveRow(row, showError=true) {
    const data = rowPayload(row);
    if (!data) return false;
    if (saveTimers.has(data.id)) { clearTimeout(saveTimers.get(data.id)); saveTimers.delete(data.id); }
    savingRows.add(data.id);
    try {
        const response = await fetch('/leads', {
            method: 'POST', body: data.payload,
            headers: {'X-Requested-With': 'XMLHttpRequest'}
        });
        if (!response.ok) throw new Error('Save failed');
        row.dataset.saved = 'yes';
        const status = document.getElementById('saveStatus');
        if (status) status.textContent = 'All changes saved';
        row.style.outline = '2px solid #b7e4c7';
        setTimeout(() => { row.style.outline = ''; }, 900);
        return true;
    } catch (error) {
        const status = document.getElementById('saveStatus');
        if (status) status.textContent = 'Save failed — do not refresh yet';
        if (showError) alert('Bhau, save zala nahi. CMD madhla error check kar. Badal page refresh karun ghalavu nakos.');
        return false;
    } finally { savingRows.delete(data.id); }
}

document.querySelectorAll('tbody tr').forEach(row => {
    row.querySelectorAll('input[name], select[name], textarea[name]').forEach(field => {
        const scheduleSave = () => {
            const id = row.querySelector('.rowSelect')?.value;
            if (!id) return;
            if (saveTimers.has(id)) clearTimeout(saveTimers.get(id));
            saveTimers.set(id, setTimeout(() => saveRow(row), 500));
        };
        const eventName = field.tagName === 'SELECT' || field.type === 'date' ? 'change' : 'input';
        field.addEventListener(eventName, scheduleSave);
        field.addEventListener('blur', () => saveRow(row));
    });
});

// Ctrl+S saves every visible row in one go.
document.addEventListener('keydown', async event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        const rows = Array.from(document.querySelectorAll('tbody tr')).filter(row => row.querySelector('.rowSelect'));
        const results = await Promise.all(rows.map(row => saveRow(row, false)));
        if (results.every(Boolean)) alert('Order Sheet save zali.');
        else alert('Kahi changes save zale nahit. CMD madhla error check kar.');
    }
});

document.getElementById('selectAll').addEventListener('change', function() {
    document.querySelectorAll('.rowSelect').forEach(function(cb) {
        cb.checked = document.getElementById('selectAll').checked;
    });
});

function deleteSelected() {
    const selected = Array.from(document.querySelectorAll('.rowSelect:checked'))
        .map(cb => cb.value);

    if (selected.length === 0) {
        alert('Please select at least one row.');
        return;
    }

    if (!confirm('Delete ' + selected.length + ' selected leads?')) return;

    Promise.all(selected.map(id =>
        fetch('/leads/delete/' + id, { method: 'POST' }).then(response => {
            if (!response.ok) throw new Error('Delete failed for row ' + id);
            return response;
        })
    )).then(() => location.reload()).catch(() => alert('Delete zala nahi. CMD madhla error check kar.'));
}
</script>

    </body>
    </html>
    """, rows=rows, columns=columns,
       today_iso=datetime.now().strftime("%Y-%m-%d"),
       today_dmy=datetime.now().strftime("%d-%m-%Y"),
       today_slash=datetime.now().strftime("%d/%m/%Y"),
       advisor_options=advisor_options, advisor_filter=advisor_filter,
       amount_summary=amount_summary, total_sale=total_sale)


@app.route("/edit-history")
def edit_history():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_edit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT
        )
    """)
    rows = conn.execute("SELECT * FROM lead_edit_history ORDER BY id DESC LIMIT 1000").fetchall()
    conn.close()
    return render_template_string("""
    <!doctype html><html><head><title>Edit History</title>
    <style>body{font-family:Arial;background:#f4f7f5;padding:20px}h2{color:#176B45}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #ddd;padding:8px;text-align:left}th{background:#176B45;color:white} .wrap{overflow-x:auto}</style>
    </head><body><h2>Order Sheet — Edit History</h2>
    <p>Recent changes (up to 1,000 records). Individual staff names require separate logins, which are not configured yet.</p>
    <p><a href="/leads">← Back to Order Sheet</a></p><div class="wrap"><table><thead><tr><th>Date & Time</th><th>Changed By</th><th>Order / Row ID</th><th>Field</th><th>Old Value</th><th>New Value</th></tr></thead><tbody>
    {% for r in rows %}<tr><td>{{ r['changed_at'] }}</td><td>{{ r['changed_by'] }}</td><td>{{ r['lead_id'] }}</td><td>{{ r['field_name'] }}</td><td>{{ r['old_value'] }}</td><td>{{ r['new_value'] }}</td></tr>{% endfor %}
    {% if not rows %}<tr><td colspan="6">No edits recorded yet.</td></tr>{% endif %}
    </tbody></table></div></body></html>
    """, rows=rows)


@app.route("/post-export")
def post_export():
    from openpyxl import load_workbook
    from datetime import datetime, date

    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    if not from_date or not to_date:
        return "Post Export sathi From Date ani To Date nivda. Mag Order Sheet madhun Post Export daba.", 400
    try:
        start_date = datetime.strptime(from_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        return "Date format barobar nahi. Date filter punha nivda.", 400
    if start_date > end_date:
        return "From Date hi To Date chya aadhi asavi.", 400

    folder = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(folder, "india_post_template.xlsx")
    if not os.path.exists(template_path):
        return "india_post_template.xlsx file CRM folder madhye sapadli nahi.", 404

    def parse_saved_date(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text_value = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(text_value, fmt).date()
            except ValueError:
                pass
        return None

    conn = get_db()
    try:
        all_rows = conn.execute("""
            SELECT l.*, o.payment_status AS linked_payment_status,
                   o.grand_total AS linked_grand_total, o.customer AS linked_customer,
                   o.address AS linked_address, o.state AS linked_state,
                   o.district_taluka AS linked_district_taluka,
                   o.pincode AS linked_pincode, o.phone AS linked_phone
            FROM leads l
            LEFT JOIN orders o ON o.id = l.order_id
            WHERE TRIM(COALESCE(l.confirmation,'')) = 'Confirm'
            ORDER BY l.id
        """).fetchall()
    finally:
        conn.close()

    # SQLite date() cannot read every date format used in older CRM rows.
    rows = []
    for item in all_rows:
        saved_date = parse_saved_date(item["date"])
        if saved_date and start_date <= saved_date <= end_date:
            rows.append(item)
    if not rows:
        return "Ya date range madhye Confirm entries sapadlya nahit. Order Sheet madhye Confirmation = Confirm ani date range tapasa.", 404

    wb = load_workbook(template_path)
    if "ArticleDetails" not in wb.sheetnames:
        return "Template madhye ArticleDetails sheet sapadli nahi.", 400
    ws = wb["ArticleDetails"]

    # In the supplied India Post workbook, the real column headings are on row 10,
    # not row 6. Keep rows 1-10 (sender/customer details and headers) intact.
    header_row = 10
    first_data_row = 11
    headers = {
        str(ws.cell(header_row, col).value).strip().upper(): col
        for col in range(1, ws.max_column + 1)
        if ws.cell(header_row, col).value is not None
    }
    if "BARCODE NO" not in headers or "PHYSICAL WEIGHT" not in headers or "RECEIVER NAME" not in headers:
        return "India Post template cha header row olakhata ala nahi; original Neww (1).xlsx template vapra.", 400

    # Remove only previous article rows, never the template headings.
    if ws.max_row >= first_data_row:
        ws.delete_rows(first_data_row, ws.max_row - first_data_row + 1)

    ws["D4"] = datetime.now().date()
    def put(row_num, header, value):
        col = headers.get(header.upper())
        if col:
            ws.cell(row_num, col).value = value

    def district_only(value):
        """Return only the district part from common Taluka/District formats."""
        if value is None:
            return ""
        text_value = str(value).strip()
        if not text_value:
            return ""
        import re
        text_value = re.sub(r"\s+", " ", text_value).strip()

        # Explicit district label wins, e.g. "Taluka: Haveli, District: Pune".
        match = re.search(r"\bdistrict\s*[:=-]?\s*([^,;/|]+)", text_value, re.IGNORECASE)
        if match:
            district = re.sub(r"\b(state|maharashtra|taluka|tal|dist)\b.*$", "", match.group(1), flags=re.IGNORECASE).strip(" :-,.")
            if district:
                return district

        # Remove common labels, then use the last part of Taluka/District strings.
        text_value = re.sub(r"^\s*(taluka|tal|district|dist)\s*[:=-]\s*", "", text_value, flags=re.IGNORECASE)
        for separator in ("/", ",", "|", " - ", " – ", "\\"):
            if separator in text_value:
                parts = [part.strip() for part in text_value.split(separator) if part.strip()]
                if parts:
                    candidate = parts[-1]
                    candidate = re.sub(r"^\s*(district|dist)\s*[:=-]?\s*", "", candidate, flags=re.IGNORECASE).strip()
                    candidate = re.sub(r",?\s*(Maharashtra|India)$", "", candidate, flags=re.IGNORECASE).strip()
                    return candidate

        # If a state/country was appended without a separator pattern, remove it.
        text_value = re.sub(r",?\s*(Maharashtra|India)$", "", text_value, flags=re.IGNORECASE).strip()
        return text_value

    for row_num, item in enumerate(rows, start=first_data_row):
        # Prefer values in the Order Sheet; fall back to the linked order when blank.
        name = item["farmer_name"] or item["linked_customer"] or ""
        mobile = item["mob_no"] or item["linked_phone"] or ""
        pincode = item["pin"] or item["linked_pincode"] or ""
        address1 = item["add1"] or item["linked_address"] or ""
        address2 = item["add2"] or ""
        amount = item["total_amount"] or item["linked_grand_total"] or ""
        district_source = item["linked_district_taluka"] or item["add2"] or ""
        city = district_only(district_source)

        put(row_num, "SR.NO", row_num - first_data_row + 1)
        # These are deliberately blank for manual entry at the post office.
        put(row_num, "BARCODE NO", None)
        put(row_num, "PHYSICAL WEIGHT", None)
        put(row_num, "RECEIVER CITY", city)
        put(row_num, "RECEIVER PINCODE", pincode)
        put(row_num, "RECEIVER NAME", name)
        put(row_num, "RECEIVER ADD LINE 1", address1)
        put(row_num, "RECEIVER ADD LINE 2", address2)
        put(row_num, "RECEIVER MOBILE NO", mobile)
        put(row_num, "VALUE FOR CODR/COD", amount)
        put(row_num, "SENDER MOBILE NO", "8793960084")
        put(row_num, "SENDER NAME", "Shubham Baharte")
        put(row_num, "SENDER COMPANY NAME", "KSP BIO")
        put(row_num, "SENDER CITY", "Pune")
        put(row_num, "SENDER STATE/UT", "Maharashtra")
        put(row_num, "SENDER PINCODE", "411048")
        put(row_num, "SENDER EMAILID", "ksp.biio@gmail.com")
        put(row_num, "SENDER ALT CONTACT", "9226604590")
        put(row_num, "SENDER ADD LINE 1", "Space 31 office no. 102 Khadi Machine Chowk Kondhwa")
        put(row_num, "SENDER ADD LINE 2", "Kondhwa, Pune 411048")

    output_path = os.path.join(folder, "india_post_export.xlsx")
    wb.save(output_path)
    return send_file(output_path, as_attachment=True, download_name="india_post_export.xlsx")


if __name__ == "__main__":
    app.run(debug=True)
