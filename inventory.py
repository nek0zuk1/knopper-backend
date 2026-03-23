from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from datetime import datetime, timedelta
from extensions import mysql

inventory_bp = Blueprint('inventory', __name__)


# GET ALL PRODUCTS
@inventory_bp.route('/get-all-products', methods=['GET'])
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
@inventory_bp.route('/inventory/branch/<int:branch_id>', methods=['GET'])
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
@inventory_bp.route('/inventory/add-products', methods=['POST'])
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

        cur.execute("UPDATE PRODUCTS SET total_stock_quantity = IFNULL(total_stock_quantity, 0) + %s WHERE product_id = %s", (quantity, product_id))

        mysql.connection.commit()
        return jsonify({"message": f"Product added to gondola '{gondola_code}'."}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#get all products in a specific gondola
@inventory_bp.route('/inventory/gondola/<string:gondola_code>', methods=['GET'])
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
@inventory_bp.route('/inventory/remove-expired', methods=['POST'])
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
@inventory_bp.route('/inventory/search', methods=['GET'])
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

@inventory_bp.route('/inventory/near-expiry', methods=['GET'])
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


# route for admmin monitoring ---------------
# VIEW STOCK AUDIT LOG
@inventory_bp.route('/admin/audit-log/<int:branch_id>', methods=['GET'])
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

#for testing only
@inventory_bp.route('/inventory/products', methods=['GET'])
@jwt_required()
def get_the_products():
    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT * FROM PRODUCTS ORDER BY product_name_official ASC")
        
        columns = [column[0] for column in cur.description]
        results = []
        
        for row in cur.fetchall():
            results.append(dict(zip(columns, row)))

        return jsonify({
            "status": "success",
            "count": len(results),
            "products": results
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()