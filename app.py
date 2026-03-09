import pymysql
pymysql.install_as_MySQLdb()
import os
from flask import Flask, request, jsonify
from flask_mysqldb import MySQL
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, get_jwt
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

app.config['MYSQL_HOST'] = os.getenv('DB_HOST')
app.config['MYSQL_USER'] = os.getenv('DB_USER')
app.config['MYSQL_PASSWORD'] = os.getenv('DB_PASSWORD') 
app.config['MYSQL_DB'] = os.getenv('DB_NAME')
app.config['MYSQL_PORT'] = int(os.getenv('DB_PORT'))
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET') 
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=12)

mysql = MySQL(app)
bcrypt = Bcrypt(app)
jwt = JWTManager(app)

# ROUTE: CREATE USER 
@app.route('/create-user', methods=['POST'])
@jwt_required()
def create_user():
    # --- 1. GET ROLE OF CURRENT USER ---
    claims = get_jwt()
    current_role = claims['role']

    # --- 2. GET DATA FROM POSTMAN ---
    data = request.json
    target_role = data.get('role')
    u_id = data.get('user_id')
    b_id = data.get('branch_id')
    uname = data.get('username')
    fname = data.get('full_name')
    pwd = data.get('password')

    # --- 3. STRICT VALIDATION: Check for missing or empty fields ---
    if not all([u_id, b_id, uname, fname, pwd, target_role]):
        return jsonify({
            "message": "Validation Error: All fields (user_id, branch_id, username, password, full_name, role) are required and cannot be empty."
        }), 400

    # --- 4. ROLE LOGIC ---
    if current_role == 'staff':
        return jsonify({"message": "Access Denied: Staff cannot create accounts"}), 403
    
    if current_role == 'manager' and target_role in ['admin', 'manager']:
        return jsonify({"message": "Access Denied: Managers can only create Staff accounts"}), 403

    cur = mysql.connection.cursor()
    try:
        # --- 5. DUPLICATE CHECK: Look for existing ID, Username, or Name IN THE SAME BRANCH ---
        cur.execute("""
            SELECT user_id, username, full_name, branch_id 
            FROM USERS 
            WHERE user_id = %s 
               OR username = %s 
               OR (full_name = %s AND branch_id = %s)
        """, (u_id, uname, fname, b_id))
        
        existing_user = cur.fetchone()
        
        # If a match is found, check exactly what triggered it
        if existing_user:
            if existing_user[0] == int(u_id):
                return jsonify({"message": f"Conflict: The user_id '{u_id}' is already in use."}), 409
            
            if existing_user[1] == uname:
                return jsonify({"message": f"Conflict: The username '{uname}' is already taken. Please choose another."}), 409
            
            # Check if the name AND the branch match
            if existing_user[2] == fname and existing_user[3] == int(b_id):
                return jsonify({"message": f"Conflict: '{fname}' is already registered at Branch {b_id}."}), 409

        # --- 6. HASH PASSWORD & INSERT TO DATABASE ---
        hashed_pwd = bcrypt.generate_password_hash(pwd).decode('utf-8')

        # Added 'is_active' and 'TRUE' to the SQL command
        cur.execute("""
            INSERT INTO USERS (user_id, branch_id, username, password_hash, full_name, role, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """, (u_id, b_id, uname, hashed_pwd, fname, target_role))
        
        mysql.connection.commit()
        return jsonify({"message": f"User {uname} created successfully!"}), 201

    except Exception as e:
        mysql.connection.rollback() 
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

# ROUTE: LOGIN
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    cur = mysql.connection.cursor()
    cur.execute("SELECT user_id, password_hash, role, branch_id, is_active FROM USERS WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()

    if user and bcrypt.check_password_hash(user[1], password):
        
        if not user[4]: 
            return jsonify({"message": "Account is inactive. Please contact your administrator."}), 403

        identity = str(user[0]) 
        
        claims = {
            "role": user[2],
            "branch": user[3]
        }
        
        token = create_access_token(identity=identity, additional_claims=claims)
     
        
        return jsonify({"access_token": token, "role": user[2]}), 200
    
    return jsonify({"message": "Invalid Credentials"}), 401

#  SETUP ADMIN 
@app.route('/setup-admin', methods=['POST'])
def setup_admin():
    # 1. SECURITY CHECK: Check for the secret header
    setup_key = request.headers.get('X-Setup-Key')
    
    if setup_key != "Knopper-Init-2026":
        return jsonify({"message": "Forbidden: Invalid Setup Key"}), 403

    # 2. CREATE ADMIN LOGIC
    data = request.json
    
    if not data or not data.get('password'):
        return jsonify({"message": "Missing password"}), 400

    hashed_pwd = bcrypt.generate_password_hash(data.get('password')).decode('utf-8')
    
    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            INSERT INTO USERS (user_id, branch_id, username, password_hash, full_name, role, is_active)
            VALUES (%s, %s, %s, %s, %s, 'admin', TRUE)
        """, (data.get('user_id'), data.get('branch_id'), data.get('username'), hashed_pwd, data.get('full_name')))
        
        mysql.connection.commit()
        return jsonify({"message": "Superadmin created and activated successfully!"}), 201

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
# GET ALL USERS
@app.route('/users', methods=['GET'])
@jwt_required()
def get_all_users():
    # Check if the requester is an Admin/Manager if you want to restrict this
    claims = get_jwt()
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403

    cur = mysql.connection.cursor()
    try:
        sql = """
            SELECT u.user_id, u.username, u.full_name, u.role, b.branch_name, u.is_active 
            FROM USERS u
            LEFT JOIN BRANCHES b ON u.branch_id = b.branch_id
            ORDER BY u.branch_id, u.role
        """
        cur.execute(sql)
        users = cur.fetchall()

        user_list = []
        for user in users:
            user_list.append({
                "user_id": user[0],
                "username": user[1],
                "full_name": user[2],
                "role": user[3],
                "branch": user[4],
                "status": "Active" if user[5] else "Inactive"
            })

        return jsonify(user_list), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
# UPDATE USER
@app.route('/update-users/<int:target_user_id>', methods=['PUT'])
@jwt_required()
def edit_user(target_user_id):
    # 1. STRICT SECURITY CHECK: Only Admins allowed
    claims = get_jwt()
    if claims.get('role') != 'admin':
        return jsonify({"message": "Access Denied: Only Administrators can edit user profiles."}), 403

    data = request.json
    if not data:
        return jsonify({"message": "No data provided to update."}), 400

    username = data.get('username')
    full_name = data.get('full_name')
    role = data.get('role')
    branch_id = data.get('branch_id')
    is_active = data.get('is_active') # True or False
    password = data.get('password')

    cur = mysql.connection.cursor()
    try:
        # 3. VERIFY USER EXISTS
        cur.execute("SELECT * FROM USERS WHERE user_id = %s", (target_user_id,))
        if not cur.fetchone():
            return jsonify({"message": f"User ID {target_user_id} not found."}), 404

        # 4. DUPLICATE CHECK (Exclude the current user we are editing)
        if username or (full_name and branch_id):
            cur.execute("""
                SELECT user_id, username, full_name, branch_id 
                FROM USERS 
                WHERE user_id != %s 
                  AND (username = %s OR (full_name = %s AND branch_id = %s))
            """, (target_user_id, username, full_name, branch_id))
            
            conflict = cur.fetchone()
            if conflict:
                if conflict[1] == username:
                    return jsonify({"message": f"Conflict: Username '{username}' is already taken."}), 409
                if conflict[2] == full_name and conflict[3] == int(branch_id):
                    return jsonify({"message": f"Conflict: '{full_name}' already exists in Branch {branch_id}."}), 409

        # 5. DYNAMICALLY BUILD THE UPDATE QUERY
        update_fields = []
        update_values = []

        if username:
            update_fields.append("username = %s")
            update_values.append(username)
        if full_name:
            update_fields.append("full_name = %s")
            update_values.append(full_name)
        if role:
            update_fields.append("role = %s")
            update_values.append(role)
        if branch_id:
            update_fields.append("branch_id = %s")
            update_values.append(branch_id)
        if is_active is not None:  # is_active could be False, so we check 'is not None'
            update_fields.append("is_active = %s")
            update_values.append(is_active)
        if password:
            hashed_pwd = bcrypt.generate_password_hash(password).decode('utf-8')
            update_fields.append("password_hash = %s")
            update_values.append(hashed_pwd)

        if not update_fields:
            return jsonify({"message": "No valid fields provided to update."}), 400

        # Add the target_user_id to the very end of our values list for the WHERE clause
        update_values.append(target_user_id)

        # Assemble the final SQL string
        sql = f"UPDATE USERS SET {', '.join(update_fields)} WHERE user_id = %s"
        
        cur.execute(sql, tuple(update_values))
        mysql.connection.commit()

        return jsonify({"message": f"User ID {target_user_id} updated successfully!"}), 200

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

# ROUTE: For PRODUCTS-----------------------------------------

# GET ALL PRODUCTS
@app.route('/get-all-products', methods=['GET'])
@jwt_required()
def get_all_products():
    cur = mysql.connection.cursor()
    try:
      
        sql = """
            SELECT product_id, product_name_official 
            FROM PRODUCTS 
            ORDER BY product_name_official ASC
        """
        cur.execute(sql)
        products = cur.fetchall()

       
        product_list = []
        for prod in products:
            product_list.append({
                "product_id": prod[0],
                "product_name_official": prod[1]
                
            })

        return jsonify(product_list), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

# ROUTE: FOR INVENTORY

#get all inventory
@app.route('/inventory/branch/<int:branch_id>', methods=['GET'])
@jwt_required()
def get_branch_inventory(branch_id):
    cur = mysql.connection.cursor()
    try:
        sql = """
            SELECT 
                bi.inventory_id,
                p.product_id,
                p.product_name_official,
                p.category_type,
                bi.batch_number,
                bi.expiry_date,
                bi.quantity_on_hand,
                p.price_regular
            FROM BRANCH_INVENTORY bi
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            WHERE bi.branch_id = %s
            ORDER BY p.product_name_official ASC, bi.expiry_date ASC
        """
        cur.execute(sql, (branch_id,))
        inventory_items = cur.fetchall()

        inventory_list = []
        for item in inventory_items:
            inventory_list.append({
                "inventory_id": item[0],
                "product_id": item[1],
                "product_name": item[2],
                "category": item[3],
                "batch_number": item[4],
                # Dates need to be converted to strings for JSON formatting
                "expiry_date": item[5].strftime('%Y-%m-%d') if item[5] else None,
                "quantity_on_hand": item[6],
                "price": float(item[7]) if item[7] else 0.00
            })

        return jsonify(inventory_list), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# ROUTE: ADD PRODUCT TO SHELF	
@app.route('/inventory/add-products', methods=['POST'])
@jwt_required()
def add_product_to_shelf():
    current_user_id = get_jwt_identity() 
    claims = get_jwt()
    current_branch_id = claims['branch'] 

    data = request.json
    product_id = data.get('product_id')
    gondola_code = data.get('gondola_code')
    batch_number = data.get('batch_number')
    expiry_date = data.get('expiry_date')
    quantity = data.get('quantity')

    if not all([product_id, gondola_code, batch_number, expiry_date, quantity]):
        return jsonify({"message": "Missing required fields"}), 400

    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT gondola_id FROM GONDOLAS WHERE gondola_code = %s AND branch_id = %s", (gondola_code, current_branch_id))
        gondola = cur.fetchone()
        if not gondola:
            return jsonify({"message": f"Gondola '{gondola_code}' not found."}), 404
        gondola_id = gondola[0]

        # INSERT INTO INVENTORY
        sql_insert = """
            INSERT INTO BRANCH_INVENTORY 
            (branch_id, product_id, gondola_id, reorder_level, target_stock_level, batch_number, expiry_date, quantity_on_hand)
            VALUES (%s, %s, %s, 10, 100, %s, %s, %s)
        """
        cur.execute(sql_insert, (current_branch_id, product_id, gondola_id, batch_number, expiry_date, quantity))
        
        # Get the ID of the new inventory record to link the adjustment
        inventory_id = cur.lastrowid

        # LOG THE AUDIT TRAIL (STOCK ADJUSTMENT)
        sql_audit = """
            INSERT INTO STOCK_ADJUSTMENTS (inventory_id, user_id, adjustment_type, quantity_adjusted, date_adjusted, remarks)
            VALUES (%s, %s, 'STOCK_IN', %s, %s, %s)
        """
        remarks = f"add supply in gandola {gondola_code}"
        cur.execute(sql_audit, (inventory_id, current_user_id, quantity, datetime.now(), remarks))

        # 3. Update Global Total
        cur.execute("UPDATE PRODUCTS SET total_stock_quantity = IFNULL(total_stock_quantity, 0) + %s WHERE product_id = %s", (quantity, product_id))

        mysql.connection.commit()
        return jsonify({"message": f"Product added to gondola '{gondola_code}'."}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#get all products in a specific gondola
@app.route('/inventory/gondola/<string:gondola_code>', methods=['GET'])
@jwt_required()
def get_gondola_inventory(gondola_code):
    
    claims = get_jwt()
    current_branch_id = claims['branch']

    cur = mysql.connection.cursor()
    try:
        
        sql = """
            SELECT 
                bi.inventory_id,
                p.product_id,
                p.product_name_official,
                p.category_type,
                bi.batch_number,
                bi.expiry_date,
                bi.quantity_on_hand
            FROM BRANCH_INVENTORY bi
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            JOIN GONDOLAS g ON bi.gondola_id = g.gondola_id
            WHERE g.gondola_code = %s AND bi.branch_id = %s
            ORDER BY p.product_name_official ASC, bi.expiry_date ASC
        """
        
        cur.execute(sql, (gondola_code, current_branch_id))
        items = cur.fetchall()

        if not items:
            return jsonify({"message": f"No products found in gondola '{gondola_code}' at your branch."}), 404

        gondola_stock = []
        for item in items:
            gondola_stock.append({
                "inventory_id": item[0],
                "product_id": item[1],
                "product_name": item[2],
                "category": item[3],
                "batch_number": item[4],
                "expiry_date": item[5].strftime('%Y-%m-%d') if item[5] else None,
                "quantity_on_hand": item[6]
            })

        return jsonify(gondola_stock), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#  Remove PRODUCT
@app.route('/inventory/remove-expired', methods=['POST'])
@jwt_required()
def remove_expired_stock():
    current_user_id = get_jwt_identity()
    claims = get_jwt()
    current_branch_id = claims['branch']

    data = request.json
    product_id = data.get('product_id')
    gondola_code = data.get('gondola_code')
    quantity_to_remove = data.get('quantity')

    if not all([product_id, gondola_code, quantity_to_remove]) or quantity_to_remove <= 0:
        return jsonify({"message": "Missing fields or invalid quantity."}), 400

    cur = mysql.connection.cursor()
    try:
        sql_find = """
            SELECT bi.inventory_id, bi.quantity_on_hand, bi.batch_number
            FROM BRANCH_INVENTORY bi
            JOIN GONDOLAS g ON bi.gondola_id = g.gondola_id
            WHERE bi.product_id = %s AND g.gondola_code = %s AND bi.branch_id = %s
            ORDER BY bi.expiry_date ASC LIMIT 1
        """
        cur.execute(sql_find, (product_id, gondola_code, current_branch_id))
        item = cur.fetchone()
        
        if not item:
            return jsonify({"message": "Product not found."}), 404
            
        inventory_id, current_qty, batch_num = item[0], item[1], item[2]

        if quantity_to_remove > current_qty:
            return jsonify({"message": "Insufficient stock."}), 400

        # LOG THE AUDIT TRAIL (STOCK ADJUSTMENT)
        sql_audit = """
            INSERT INTO STOCK_ADJUSTMENTS (inventory_id, user_id, adjustment_type, quantity_adjusted, date_adjusted, remarks)
            VALUES (%s, %s, 'DISPOSAL', %s, %s, %s)
        """
        remarks = f"Expired stock removed from {gondola_code} (Batch: {batch_num})"
        cur.execute(sql_audit, (inventory_id, current_user_id, quantity_to_remove, datetime.now(), remarks))

        # UPDATE INVENTORY
        new_qty = current_qty - quantity_to_remove
        if new_qty > 0:
            cur.execute("UPDATE BRANCH_INVENTORY SET quantity_on_hand = %s WHERE inventory_id = %s", (new_qty, inventory_id))
        else:
            cur.execute("DELETE FROM BRANCH_INVENTORY WHERE inventory_id = %s", (inventory_id,))

        #  Update Global Total
        cur.execute("UPDATE PRODUCTS SET total_stock_quantity = total_stock_quantity - %s WHERE product_id = %s", (quantity_to_remove, product_id))

        mysql.connection.commit()
        return jsonify({"message": f"Stock removed and logged as Disposal."}), 200
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#  SEARCH PRODUCT BY NAME
#http://127.0.0.1:5000/inventory/search?name=name of product 
@app.route('/inventory/search', methods=['GET'])
@jwt_required()
def search_product():
    claims = get_jwt()
    current_branch_id = claims['branch']
    
    # Get the search term from the URL parameter (e.g., /search?name=paracetamol)
    search_query = request.args.get('name', '')

    if not search_query:
        return jsonify({"message": "Please provide a product name to search for."}), 400

    cur = mysql.connection.cursor()
    try:
    
        sql = """
            SELECT 
                p.product_id,
                p.product_name_official, 
                bi.batch_number, 
                bi.expiry_date, 
                bi.quantity_on_hand, 
                g.gondola_code
            FROM BRANCH_INVENTORY bi
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            JOIN GONDOLAS g ON bi.gondola_id = g.gondola_id
            WHERE bi.branch_id = %s 
              AND p.product_name_official LIKE %s
            ORDER BY p.product_name_official ASC, bi.expiry_date ASC
        """
        like_pattern = f"%{search_query}%"
        cur.execute(sql, (current_branch_id, like_pattern))
        results = cur.fetchall()

        if not results:
            return jsonify({"message": f"No products matching '{search_query}' found in your branch."}), 404

        # 3. Format the results
        search_results = []
        for row in results:
            search_results.append({
                "product_id": row[0],
                "product_name": row[1],
                "batch_number": row[2],
                "expiry_date": row[3].strftime('%Y-%m-%d') if row[3] else None,
                "quantity": row[4],
                "location": row[5]
            })

        return jsonify({
            "search_term": search_query,
            "results_found": len(search_results),
            "items": search_results
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

# GET NEAR EXPIRY PRODUCTS near30 days

@app.route('/inventory/near-expiry', methods=['GET'])
@jwt_required()
def get_near_expiry():
    claims = get_jwt()
    current_branch_id = claims['branch']

    today = datetime.now().date()
    threshold_date = today + timedelta(days=30)

    cur = mysql.connection.cursor()
    try:

        sql = """
            SELECT 
                p.product_name_official, 
                bi.batch_number, 
                bi.expiry_date, 
                bi.quantity_on_hand, 
                g.gondola_code
            FROM BRANCH_INVENTORY bi
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            JOIN GONDOLAS g ON bi.gondola_id = g.gondola_id
            WHERE bi.branch_id = %s 
              AND bi.expiry_date <= %s
            ORDER BY bi.expiry_date ASC
        """
        cur.execute(sql, (current_branch_id, threshold_date))
        results = cur.fetchall()

        expiry_list = []
        for row in results:
            expiry_date = row[2]
            # Calculate status: Expired vs Expiring Soon
            status = "EXPIRED" if expiry_date < today else "EXPIRING SOON"
            
            expiry_list.append({
                "product_name": row[0],
                "batch_number": row[1],
                "expiry_date": expiry_date.strftime('%Y-%m-%d'),
                "quantity": row[3],
                "location": row[4],
                "status": status
            })

        return jsonify({
            "branch_id": current_branch_id,
            "report_date": today.strftime('%Y-%m-%d'),
            "total_count": len(expiry_list),
            "items": expiry_list
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# route for admmin monitoring -----------------
# VIEW STOCK AUDIT LOG
@app.route('/admin/audit-log/<int:branch_id>', methods=['GET'])
@jwt_required()
def get_audit_log(branch_id):
    claims = get_jwt()
    if claims.get('role') != 'admin':
        return jsonify({"message": "Access Denied: Administrator privileges required"}), 403

    cur = mysql.connection.cursor()
    try:
    
        sql = """
            SELECT 
                sa.date_adjusted, 
                u.full_name, 
                p.product_name_official, 
                sa.adjustment_type, 
                sa.quantity_adjusted, 
                sa.remarks
            FROM STOCK_ADJUSTMENTS sa
            JOIN USERS u ON sa.user_id = u.user_id
            JOIN BRANCH_INVENTORY bi ON sa.inventory_id = bi.inventory_id
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            WHERE bi.branch_id = %s
            ORDER BY sa.date_adjusted DESC
        """
        cur.execute(sql, (branch_id,))
        logs = cur.fetchall()

        audit_trail = []
        for log in logs:
            audit_trail.append({
                "date_time": log[0].strftime('%Y-%m-%d %H:%M:%S'),
                "performed_by": log[1],
                "product": log[2],
                "action_type": log[3],
                "quantity": log[4],
                "details": log[5]
            })

        return jsonify({
            "branch_id": branch_id,
            "total_entries": len(audit_trail),
            "logs": audit_trail
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close



# ----------------------------------POS----------------------


# ROUTE: POS - PROCESS SALE 
@app.route('/pos/checkout', methods=['POST'])
@jwt_required()
def process_checkout():
    current_user_id = get_jwt_identity()
    claims = get_jwt()
    current_branch_id = claims['branch']

   
    if claims.get('role') not in ['admin', 'cashier', 'manager']:
        return jsonify({"message": "Access Denied."}), 403

    data = request.json
    cart = data.get('cart') # format: [{"barcode": "4801234567", "quantity": 2}]
    payment_method = data.get('payment_method', 'CASH')
    customer_type = data.get('customer_type', 'REGULAR')

    if not cart or not isinstance(cart, list) or len(cart) == 0:
        return jsonify({"message": "Cart is empty or invalid format."}), 400

    cur = mysql.connection.cursor()
    try:
      
        sale_date = datetime.now()
        cur.execute("""
            INSERT INTO SALES_HEADERS (branch_id, user_id, sale_date, total_amount, payment_method, customer_type)
            VALUES (%s, %s, %s, 0.00, %s, %s)
        """, (current_branch_id, current_user_id, sale_date, payment_method, customer_type))
        
        sale_id = cur.lastrowid
        grand_total = 0.0

        # PROCESS EACH ITEM IN THE CART
        for item in cart:
            scanned_barcode = item.get('barcode') 
            qty_to_sell = item.get('quantity')

          
            cur.execute("""
                SELECT bi.inventory_id, bi.quantity_on_hand, p.price_regular, p.product_id
                FROM PRODUCT_BARCODES pb
                JOIN PRODUCTS p ON pb.product_id = p.product_id
                JOIN BRANCH_INVENTORY bi ON p.product_id = bi.product_id
                WHERE pb.barcode_value = %s AND bi.branch_id = %s AND bi.quantity_on_hand > 0
                ORDER BY bi.expiry_date ASC LIMIT 1
            """, (scanned_barcode, current_branch_id))
            
            stock_item = cur.fetchone()
            if not stock_item:
                raise Exception(f"Barcode '{scanned_barcode}' is out of stock or not registered in your branch.")

            # Retrieve stock item details
            inv_id = stock_item[0]
            current_qty = stock_item[1]
            price = stock_item[2]
            prod_id = stock_item[3] 
            
            if qty_to_sell > current_qty:
                raise Exception(f"Insufficient stock for Barcode '{scanned_barcode}'. Only {current_qty} left.")

            # Calculate line total for this specific item
            line_total = float(price) * qty_to_sell
            grand_total += line_total

            # Deduct from Branch Inventory
            new_qty = current_qty - qty_to_sell
            if new_qty > 0:
                cur.execute("UPDATE BRANCH_INVENTORY SET quantity_on_hand = %s WHERE inventory_id = %s", (new_qty, inv_id))
            else:
                cur.execute("DELETE FROM BRANCH_INVENTORY WHERE inventory_id = %s", (inv_id,))

            # Deduct from Global Total
            cur.execute("UPDATE PRODUCTS SET total_stock_quantity = total_stock_quantity - %s WHERE product_id = %s", (qty_to_sell, prod_id))

            # Record in SALES_DETAILS (Line Items)
            cur.execute("""
                INSERT INTO SALES_DETAILS (sale_id, inventory_id, quantity_sold, price_at_sale, discount_applied)
                VALUES (%s, %s, %s, %s, 0.00)
            """, (sale_id, inv_id, qty_to_sell, price))

        # 4. UPDATE THE GRAND TOTAL ON THE HEADER
        cur.execute("UPDATE SALES_HEADERS SET total_amount = %s WHERE sale_id = %s", (grand_total, sale_id))

        mysql.connection.commit()
        
        # Output showing the total price prominently
        return jsonify({
            "status": "success",
            "message": "Checkout complete!",
            "receipt_number": sale_id,
            "items_purchased": len(cart),
            "total_price": round(grand_total, 2)
        }), 201

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        cur.close()









#----- procurement routes ---------------------------------
def next_id(cursor, table, id_col):
    """Auto-generate the next ID for any table."""
    cursor.execute(f"SELECT IFNULL(MAX({id_col}), 0) + 1 FROM {table}")
    return cursor.fetchone()[0]

# GET /procurement/<id> - Get PO details with items
@app.route('/procurement/<int:order_id>', methods=['GET'])
@jwt_required()
def get_purchase_order(order_id):
    claims = get_jwt()
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({'message': 'Access Denied'}), 403
    cur = mysql.connection.cursor()
    try:
        # Get PO Header
        cur.execute("""
            SELECT po.order_id, po.order_date, po.status, po.total_amount,
                   s.supplier_name, b.branch_name,
                   creator.full_name, approver.full_name
            FROM PURCHASE_ORDERS po
            LEFT JOIN SUPPLIERS s ON po.supplier_id = s.supplier_id
            LEFT JOIN BRANCHES b ON po.branch_id = b.branch_id
            LEFT JOIN USERS creator ON po.created_by_user_id = creator.user_id
            LEFT JOIN USERS approver ON po.approved_by_user_id = approver.user_id
            WHERE po.order_id = %s
        """, (order_id,))
        po = cur.fetchone()
        if not po:
            return jsonify({'message': 'PO not found'}), 404

        # Get PO Items
        cur.execute("""
            SELECT poi.po_item_id, p.product_name_official, 
                   poi.quantity_ordered, poi.uom, poi.cost_at_time_of_order, poi.item_status
            FROM PURCHASE_ORDER_ITEMS poi
            JOIN PRODUCTS p ON poi.product_id = p.product_id
            WHERE poi.order_id = %s
        """, (order_id,))
        items = cur.fetchall()

        return jsonify({
            'order_id': po[0],
            'order_date': po[1].strftime('%Y-%m-%d') if po[1] else None,
            'status': po[2],
            'total_amount': float(po[3]) if po[3] else 0.00,
            'supplier': po[4],
            'branch': po[5],
            'created_by': po[6],
            'approved_by': po[7],
            'items': [{
                'po_item_id': i[0],
                'product': i[1],
                'quantity': i[2],
                'uom': i[3],
                'cost': float(i[4]) if i[4] else 0.00,
                'status': i[5]
            } for i in items]
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()

# GET /purchase-orders
@app.route('/procurement', methods=['GET'])
@jwt_required()
def get_purchase_orders():
    claims = get_jwt()
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            SELECT po.order_id, po.order_date, po.status, po.total_amount,
                   s.supplier_name, b.branch_name,
                   creator.full_name, approver.full_name
            FROM PURCHASE_ORDERS po
            LEFT JOIN SUPPLIERS s ON po.supplier_id = s.supplier_id
            LEFT JOIN BRANCHES b ON po.branch_id = b.branch_id
            LEFT JOIN USERS creator ON po.created_by_user_id = creator.user_id
            LEFT JOIN USERS approver ON po.approved_by_user_id = approver.user_id
            ORDER BY po.order_date DESC
        """)
        rows = cur.fetchall()
        return jsonify([{
            "order_id": r[0],
            "order_date": r[1].strftime('%Y-%m-%d') if r[1] else None,
            "status": r[2],
            "total_amount": float(r[3]) if r[3] else 0.00,
            "supplier": r[4],
            "branch": r[5],
            "created_by": r[6],
            "approved_by": r[7]
        } for r in rows]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# POST /purchase-orders
# Send: supplier_id, branch_id, items[{product_id, quantity, cost}]
@app.route('/procurement', methods=['POST'])
@jwt_required()
def create_purchase_order():
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    data = request.json
    supplier_id = data.get('supplier_id')
    branch_id   = data.get('branch_id')
    items       = data.get('items', [])
    if not all([supplier_id, branch_id, items]):
        return jsonify({"message": "Need: supplier_id, branch_id, items"}), 400
    cur = mysql.connection.cursor()
    try:
        order_id = next_id(cur, 'PURCHASE_ORDERS', 'order_id')
        total    = sum(float(i.get('cost', 0)) * int(i.get('quantity', 0)) for i in items)
        cur.execute("""
            INSERT INTO PURCHASE_ORDERS
            (order_id, supplier_id, branch_id, created_by_user_id, order_date, status, total_amount)
            VALUES (%s, %s, %s, %s, NOW(), 'DRAFT', %s)
        """, (order_id, supplier_id, branch_id, current_user_id, total))
        for item in items:
            po_item_id = next_id(cur, 'PURCHASE_ORDER_ITEMS', 'po_item_id')
            cur.execute("""
                INSERT INTO PURCHASE_ORDER_ITEMS
                (po_item_id, order_id, product_id, quantity_ordered, uom, conversion_factor, cost_at_time_of_order, item_status)
                VALUES (%s, %s, %s, %s, 'PC', 1, %s, 'PENDING')
            """, (po_item_id, order_id, item.get('product_id'), item.get('quantity'), item.get('cost', 0)))
        mysql.connection.commit()
        return jsonify({"message": "PO created!", "order_id": order_id, "total": total}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# PUT /purchase-orders/<id>
# Send: status
@app.route('/procurement/<int:order_id>', methods=['PUT'])
@jwt_required()
def update_purchase_order(order_id):
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    data       = request.json
    new_status = data.get('status')
    if not new_status:
        return jsonify({"message": "Need: status (DRAFT / SENT / RECEIVED / CANCELLED)"}), 400
    cur = mysql.connection.cursor()
    try:
        cur.execute("UPDATE PURCHASE_ORDERS SET status=%s, approved_by_user_id=%s WHERE order_id=%s",
                    (new_status, current_user_id, order_id))
        if cur.rowcount == 0:
            return jsonify({"message": "PO not found"}), 404
        mysql.connection.commit()
        return jsonify({"message": f"PO {order_id} is now '{new_status}'"}), 200
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# POST /receiving
# Send: order_id, items[{po_item_id, quantity, batch, expiry}]
@app.route('/procurement/receive', methods=['POST'])
@jwt_required()
def receive_delivery():
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    data     = request.json
    order_id = data.get('order_id')
    items    = data.get('items', [])
    if not all([order_id, items]):
        return jsonify({"message": "Need: order_id, items"}), 400
    cur = mysql.connection.cursor()
    try:
        receipt_id = next_id(cur, 'RECEIVING_REPORTS', 'receipt_id')
        cur.execute("""
            INSERT INTO RECEIVING_REPORTS
            (receipt_id, order_id, received_by_user_id, date_received)
            VALUES (%s, %s, %s, NOW())
        """, (receipt_id, order_id, current_user_id))
        for item in items:
            receipt_item_id = next_id(cur, 'RECEIPT_ITEMS', 'receipt_item_id')
            cur.execute("""
                INSERT INTO RECEIPT_ITEMS
                (receipt_item_id, receipt_id, po_item_id, quantity_received, batch_number, expiry_date)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (receipt_item_id, receipt_id, item.get('po_item_id'),
                  item.get('quantity'), item.get('batch', 'BATCH-001'), item.get('expiry')))
            cur.execute("UPDATE PURCHASE_ORDER_ITEMS SET item_status='RECEIVED' WHERE po_item_id=%s",
                        (item.get('po_item_id'),))
        cur.execute("UPDATE PURCHASE_ORDERS SET status='RECEIVED' WHERE order_id=%s", (order_id,))
        mysql.connection.commit()
        return jsonify({"message": f"Delivery recorded for PO {order_id}!"}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# POST /transfers
# Send: from_branch_id, to_branch_id, items[{product_id, quantity, batch}]
@app.route('/procurement/transfer', methods=['POST'])
@jwt_required()
def create_transfer():
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    data        = request.json
    from_branch = data.get('from_branch_id')
    to_branch   = data.get('to_branch_id')
    items       = data.get('items', [])
    if not all([from_branch, to_branch, items]):
        return jsonify({"message": "Need: from_branch_id, to_branch_id, items"}), 400
    if from_branch == to_branch:
        return jsonify({"message": "Cannot transfer to the same branch!"}), 400
    cur = mysql.connection.cursor()
    try:
        manifest_id = next_id(cur, 'TRANSFER_MANIFEST', 'manifest_id')
        cur.execute("""
            INSERT INTO TRANSFER_MANIFEST
            (manifest_id, user_id, from_branch_id, to_branch_id, date_departed, status)
            VALUES (%s, %s, %s, %s, NOW(), 'IN_TRANSIT')
        """, (manifest_id, current_user_id, from_branch, to_branch))
        for item in items:
            transfer_item_id = next_id(cur, 'TRANSFER_ITEMS', 'transfer_item_id')
            batch = item.get('batch', 'BATCH-001')
            cur.execute("""
                INSERT INTO TRANSFER_ITEMS
                (transfer_item_id, manifest_id, product_id, batch_number, quantity_sent, quantity_received)
                VALUES (%s, %s, %s, %s, %s, 0)
            """, (transfer_item_id, manifest_id, item.get('product_id'), batch, item.get('quantity')))
            cur.execute("""
                UPDATE BRANCH_INVENTORY
                SET quantity_on_hand = quantity_on_hand - %s
                WHERE product_id=%s AND branch_id=%s AND batch_number=%s
            """, (item.get('quantity'), item.get('product_id'), from_branch, batch))
        mysql.connection.commit()
        return jsonify({"message": "Transfer created!", "manifest_id": manifest_id}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()




if __name__ == '__main__':
    app.run(debug=True)