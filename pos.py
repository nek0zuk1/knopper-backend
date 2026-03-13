from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from datetime import datetime
from extensions import mysql
from extensions import mysql, bcrypt, jwt
pos_bp = Blueprint('pos', __name__)
import json


# POS - PROCESS SALE 
@pos_bp.route('/pos/checkout', methods=['POST'])
@jwt_required()
def process_checkout():
    current_user_id = int(get_jwt_identity()) 
    claims = get_jwt()
    current_branch_id = claims['branch']

    if claims.get('role') not in ['admin', 'cashier', 'manager']:
        return jsonify({"message": "Access Denied."}), 403

    data = request.json
    cart = data.get('cart') 
    payment_method = data.get('payment_method', 'CASH')
    customer_type = data.get('customer_type', 'REGULAR').upper().strip() 
    
    amount_tendered = float(data.get('amount_tendered', 0.0))

    if not cart or not isinstance(cart, list) or len(cart) == 0:
        return jsonify({"message": "Cart is empty or invalid format."}), 400

    cur = mysql.connection.cursor()
    try:
        # GET ACTIVE SHIFT 
        cur.execute("SELECT shift_id FROM CASHIER_SHIFTS WHERE user_id = %s AND status = 'OPEN'", (current_user_id,))
        active_shift = cur.fetchone()
        
        if not active_shift:
            return jsonify({"message": "You must open a shift before processing sales."}), 403
            
        current_shift_id = active_shift[0]

        sale_date = datetime.now()
        cur.execute("""
            INSERT INTO SALES_HEADERS (branch_id, user_id, shift_id, sale_date, total_amount, tax_amount, discount_total, payment_method, customer_type)
            VALUES (%s, %s, %s, %s, 0.00, 0.00, 0.00, %s, %s)
        """, (current_branch_id, current_user_id, current_shift_id, sale_date, payment_method, customer_type))
        
        sale_id = cur.lastrowid
        
        raw_subtotal = 0.0
        total_discount = 0.0
        total_vat = 0.0
        grand_total = 0.0

        for item in cart:
            scanned_barcode = item.get('barcode') 
            manual_product_id = item.get('product_id')
            qty_to_sell = item.get('quantity')

            if scanned_barcode:
                cur.execute("""
                    SELECT bi.inventory_id, bi.quantity_on_hand, p.price_regular, p.product_id
                    FROM PRODUCT_BARCODES pb
                    JOIN PRODUCTS p ON pb.product_id = p.product_id
                    JOIN BRANCH_INVENTORY bi ON p.product_id = bi.product_id
                    WHERE pb.barcode_value = %s AND bi.branch_id = %s AND bi.quantity_on_hand > 0
                    ORDER BY bi.expiry_date ASC LIMIT 1
                """, (scanned_barcode, current_branch_id))
            elif manual_product_id:
                cur.execute("""
                    SELECT bi.inventory_id, bi.quantity_on_hand, p.price_regular, p.product_id
                    FROM BRANCH_INVENTORY bi
                    JOIN PRODUCTS p ON bi.product_id = p.product_id
                    WHERE bi.product_id = %s AND bi.branch_id = %s AND bi.quantity_on_hand > 0
                    ORDER BY bi.expiry_date ASC LIMIT 1
                """, (manual_product_id, current_branch_id))
            else:
                raise Exception("Every item must have a 'barcode' or 'product_id'.")
            
            stock_item = cur.fetchone()
            if not stock_item:
                identifier = scanned_barcode if scanned_barcode else manual_product_id
                raise Exception(f"Item '{identifier}' is out of stock or not registered.")

            inv_id, current_qty, price, prod_id = stock_item[0], stock_item[1], float(stock_item[2]), stock_item[3]

            # Prevents negative stock mathematically
            if qty_to_sell > current_qty:
                raise Exception(f"Insufficient stock for Item {prod_id}. Only {current_qty} left.")

            line_total = price * qty_to_sell
            raw_subtotal += line_total

            # Item Math
            item_discount = 0.0
            item_vat = 0.0
            item_payable = 0.0

            if customer_type == 'DISCOUNTED':
                item_discount = line_total * 0.20
                item_payable = line_total - item_discount
                item_vat = 0.00
            else: 
                item_payable = line_total
                item_vat = line_total - (line_total / 1.12)

            total_discount += item_discount
            total_vat += item_vat
            grand_total += item_payable

            new_qty = current_qty - qty_to_sell
            cur.execute("UPDATE BRANCH_INVENTORY SET quantity_on_hand = %s WHERE inventory_id = %s", (new_qty, inv_id))

            cur.execute("UPDATE PRODUCTS SET total_stock_quantity = total_stock_quantity - %s WHERE product_id = %s", (qty_to_sell, prod_id))

            cur.execute("""
                INSERT INTO SALES_DETAILS (sale_id, inventory_id, quantity_sold, price_at_sale, discount_applied)
                VALUES (%s, %s, %s, %s, %s)
            """, (sale_id, inv_id, qty_to_sell, price, item_discount))

        if payment_method == 'CASH':
            if amount_tendered < grand_total:
                raise Exception(f"Insufficient funds. Total is ₱{grand_total:.2f}, but only ₱{amount_tendered:.2f} was tendered.")
            change_due = amount_tendered - grand_total
        else:
            amount_tendered = grand_total
            change_due = 0.0

        cur.execute("""
            UPDATE SALES_HEADERS 
            SET total_amount = %s, tax_amount = %s, discount_total = %s, amount_tendered = %s, change_due = %s
            WHERE sale_id = %s
        """, (grand_total, total_vat, total_discount, amount_tendered, change_due, sale_id))
        mysql.connection.commit()
        
        return jsonify({
            "status": "success",
            "message": "Checkout complete!",
            "receipt_number": sale_id,
            "items_purchased": len(cart),
            "subtotal": round(raw_subtotal, 2),
            "discount_applied": round(total_discount, 2),
            "vat_amount": round(total_vat, 2),
            "total_paid": round(grand_total, 2),
            "amount_tendered": round(amount_tendered, 2), 
            "change_due": round(change_due, 2)            
        }), 201

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        cur.close()

# GET RECEIPT BY SALE ID

@pos_bp.route('/pos/receipt/<int:sale_id>', methods=['GET'])
@jwt_required()
def get_receipt(sale_id):
    claims = get_jwt()
    if claims.get('role') not in ['admin', 'cashier', 'manager']:
        return jsonify({"message": "Access Denied."}), 403

    cur = mysql.connection.cursor()
    try:
        header_sql = """
            SELECT 
                sh.sale_id, 
                sh.sale_date, 
                sh.total_amount, 
                sh.payment_method, 
                sh.customer_type, 
                u.full_name AS cashier_name, 
                b.branch_name,
                sh.tax_amount,
                sh.discount_total,
                sh.amount_tendered,
                sh.change_due
            FROM SALES_HEADERS sh
            JOIN USERS u ON sh.user_id = u.user_id
            JOIN BRANCHES b ON sh.branch_id = b.branch_id
            WHERE sh.sale_id = %s
        """
        cur.execute(header_sql, (sale_id,))
        header = cur.fetchone()

        if not header:
            return jsonify({"message": f"Receipt #{sale_id} not found."}), 404

       
        items_sql = """
            SELECT 
                p.product_name_official, 
                sd.quantity_sold, 
                sd.price_at_sale, 
                (sd.quantity_sold * sd.price_at_sale) AS line_total,
                sd.discount_applied
            FROM SALES_DETAILS sd
            JOIN BRANCH_INVENTORY bi ON sd.inventory_id = bi.inventory_id
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            WHERE sd.sale_id = %s
        """
        cur.execute(items_sql, (sale_id,))
        items = cur.fetchall()

        item_list = []
        raw_subtotal = 0.0
        for item in items:
            line_total = float(item[3])
            raw_subtotal += line_total
            item_list.append({
                "product_name": item[0],
                "qty": item[1],
                "unit_price": float(item[2]),
                "subtotal": line_total,
                "item_discount": float(item[4])
            })

        receipt_data = {
            "store_name": "Knopper Pharmacy",
            "branch": header[6],
            "receipt_number": f"INV-{header[0]:06d}", 
            "date": header[1].strftime('%Y-%m-%d %H:%M:%S'),
            "cashier": header[5],
            "customer_type": header[4],
            "payment_method": header[3],
            "items": item_list,
            "financials": {
                "subtotal": round(raw_subtotal, 2),
                "vat_amount": round(float(header[7]), 2),
                "discount_amount": round(float(header[8]), 2),
                "grand_total": round(float(header[2]), 2),
                "amount_tendered": round(float(header[9]), 2) if header[9] is not None else round(float(header[2]), 2),
                "change_due": round(float(header[10]), 2) if header[10] is not None else 0.00
            }
        }

        return jsonify(receipt_data), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#void transaction
@pos_bp.route('/pos/void/<int:sale_id>', methods=['POST'])
@jwt_required()
def void_transaction(sale_id):
    cashier_id = get_jwt_identity()

    # Manager credential is required to void a transaction
    data = request.json
    mgr_username = data.get('manager_username')
    mgr_password = data.get('manager_password')

    if not mgr_username or not mgr_password:
        return jsonify({"message": "Manager override credentials are required."}), 400

    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT user_id, password_hash, role, is_active FROM USERS WHERE username = %s", (mgr_username,))
        manager = cur.fetchone()

        if not manager or not bcrypt.check_password_hash(manager[1], mgr_password):
            return jsonify({"message": "Override Failed: Invalid Manager Credentials."}), 401
        
        if manager[2] not in ['admin', 'manager']:
            return jsonify({"message": "Override Denied: User is not an admin or manager."}), 403
            
        if not manager[3]:
            return jsonify({"message": "Override Denied: Manager account is currently inactive."}), 403
            
        manager_id = manager[0]

        cur.execute("SELECT total_amount, payment_method FROM SALES_HEADERS WHERE sale_id = %s", (sale_id,))
        sale = cur.fetchone()
        
        if not sale:
            return jsonify({"message": "Receipt not found."}), 404
            
        if float(sale[0]) <= 0:
            return jsonify({"message": "This transaction has already been voided."}), 400

        # --- 3. RETURN INVENTORY TO SHELF ---
        cur.execute("""
            SELECT sd.inventory_id, sd.quantity_sold, bi.product_id 
            FROM SALES_DETAILS sd
            JOIN BRANCH_INVENTORY bi ON sd.inventory_id = bi.inventory_id
            WHERE sd.sale_id = %s
        """, (sale_id,))
        items = cur.fetchall()

        for item in items:
            inv_id, qty_sold, prod_id = item[0], item[1], item[2]
            
            cur.execute("UPDATE BRANCH_INVENTORY SET quantity_on_hand = quantity_on_hand + %s WHERE inventory_id = %s", (qty_sold, inv_id))
            cur.execute("UPDATE PRODUCTS SET total_stock_quantity = total_stock_quantity + %s WHERE product_id = %s", (qty_sold, prod_id))

            cur.execute("""
                INSERT INTO STOCK_ADJUSTMENTS (inventory_id, user_id, adjustment_type, quantity_adjusted, date_adjusted, remarks)
                VALUES (%s, %s, 'VOID_RESTORE', %s, %s, %s)
            """, (inv_id, manager_id, qty_sold, datetime.now(), f"Manager Override: Voided Sale #{sale_id}"))

        # --- 4. CROSS OUT THE RECEIPT ---
        cur.execute("""
            UPDATE SALES_HEADERS 
            SET total_amount = 0, tax_amount = 0, discount_total = 0, 
                amount_tendered = 0, change_due = 0, customer_type = 'VOIDED' 
            WHERE sale_id = %s
        """, (sale_id,))

        mysql.connection.commit()
        return jsonify({
            "status": "success",
            "message": f"Receipt #{sale_id} voided successfully.",
            "authorized_by": mgr_username
        }), 200

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
#

#  SHOW DAILY SALES REPORT
#https://web-production-2c7737.up.railway.app/pos/daily-sales?date=2026-03-08   replace with date you want to view
#if no date is provided it will default to current date
@pos_bp.route('/pos/daily-sales', methods=['GET'])
@jwt_required()
def get_daily_sales():
    claims = get_jwt()
    current_branch_id = claims['branch']
    
    if claims.get('role') not in ['admin', 'manager', 'cashier']:
        return jsonify({"message": "Access Denied."}), 403

    # Accept a date from the URL (e.g., ?date=2026-03-10), otherwise default to today
    target_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

    cur = mysql.connection.cursor()
    try:
        # GET THE GRAND TOTALS FOR THE DAY
        cur.execute("""
            SELECT 
                COUNT(sale_id) AS total_transactions,
                IFNULL(SUM(total_amount), 0) AS total_revenue,
                IFNULL(SUM(tax_amount), 0) AS total_vat,
                IFNULL(SUM(discount_total), 0) AS total_discounts
            FROM SALES_HEADERS
            WHERE branch_id = %s AND DATE(sale_date) = %s
        """, (current_branch_id, target_date))
        
        summary = cur.fetchone()

        cur.execute("""
            SELECT payment_method, IFNULL(SUM(total_amount), 0) 
            FROM SALES_HEADERS
            WHERE branch_id = %s AND DATE(sale_date) = %s
            GROUP BY payment_method
        """, (current_branch_id, target_date))
        
        payment_breakdown = cur.fetchall()
        
        payments = {}
        for row in payment_breakdown:
            payments[row[0]] = round(float(row[1]), 2)

        report = {
            "branch_id": current_branch_id,
            "report_date": target_date,
            "summary": {
                "total_transactions": summary[0],
                "total_revenue": round(float(summary[1]), 2),
                "total_vat_collected": round(float(summary[2]), 2),
                "total_discounts_given": round(float(summary[3]), 2)
            },
            "payment_breakdown": payments
        }

        return jsonify(report), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#SHIFT MANAGEMENT - OPEN SHIFT
@pos_bp.route('/pos/shift/open', methods=['POST'])
@jwt_required()
def open_shift():
    user_id = int(get_jwt_identity()) 
    claims = get_jwt()
    branch_id = claims['branch']
    
    if claims.get('role') not in ['admin', 'manager', 'cashier']:
        return jsonify({"message": "Access Denied: Only authorized staff can open a shift."}), 403
     
    data = request.json
    starting_cash = float(data.get('starting_cash', 0.00))
    provided_password = data.get('password')

    if not provided_password:
        return jsonify({"message": "Security Verification: Please provide your password to open the shift."}), 400

    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT password_hash FROM USERS WHERE user_id = %s", (user_id,))
        user_record = cur.fetchone()

        if not user_record or not bcrypt.check_password_hash(user_record[0], provided_password):
            return jsonify({"message": "Verification Failed: Incorrect password."}), 401

        cur.execute("SELECT shift_id FROM CASHIER_SHIFTS WHERE user_id = %s AND status = 'OPEN'", (user_id,))
        if cur.fetchone():
            return jsonify({"message": "You already have an open shift! Please close the current shift before opening a new one."}), 400

        cur.execute("""
            INSERT INTO CASHIER_SHIFTS (user_id, branch_id, start_time, starting_cash, status)
            VALUES (%s, %s, NOW(), %s, 'OPEN')
        """, (user_id, branch_id, starting_cash))
        
        shift_id = cur.lastrowid
        mysql.connection.commit()
        
        return jsonify({
            "status": "success",
            "message": "Identity verified. Shift opened successfully!",
            "shift_id": shift_id,
            "starting_cash": starting_cash
        }), 201

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#SHIFT MANAGEMENT - CLOSE SHIFT
@pos_bp.route('/pos/shift/close', methods=['POST'])
@jwt_required()
def close_shift():
    user_id = int(get_jwt_identity())
    data = request.json
    
    actual_cash = float(data.get('actual_cash', 0.00))

    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT shift_id, starting_cash, start_time, branch_id FROM CASHIER_SHIFTS WHERE user_id = %s AND status = 'OPEN'", (user_id,))
        shift = cur.fetchone()
        
        if not shift:
            return jsonify({"message": "No open shift found. You must open a shift first."}), 404
            
        shift_id = shift[0]
        starting_cash = float(shift[1])
        start_time = shift[2]
        branch_id = shift[3]

        cur.execute("""
            SELECT IFNULL(SUM(total_amount), 0) 
            FROM SALES_HEADERS 
            WHERE shift_id = %s AND payment_method = 'CASH' AND customer_type != 'VOIDED'
        """, (shift_id,))
        total_cash_sales = float(cur.fetchone()[0])
        
        cur.execute("""
            SELECT IFNULL(SUM(total_refund_amount), 0)
            FROM SALES_RETURNS
            WHERE branch_id = %s AND return_date >= %s
        """, (branch_id, start_time))
        total_refunds = float(cur.fetchone()[0])

        expected_cash = (starting_cash + total_cash_sales) - total_refunds
        discrepancy = actual_cash - expected_cash

        cur.execute("""
            UPDATE CASHIER_SHIFTS 
            SET end_time = NOW(), expected_cash = %s, actual_cash = %s, discrepancy = %s, status = 'CLOSED'
            WHERE shift_id = %s
        """, (expected_cash, actual_cash, discrepancy, shift_id))
        
        # CLEAN UP ABANDONED SUSPENDED TRANSACTIONS
        cur.execute("DELETE FROM SUSPENDED_TRANSACTIONS WHERE user_id = %s", (user_id,))
        cleared_carts = cur.rowcount

        mysql.connection.commit()
        
        return jsonify({
            "status": "success",
            "message": "Shift closed successfully!",
            "shift_id": shift_id,
            "summary": {
                "starting_cash": starting_cash,
                "cash_sales": total_cash_sales,
                "cash_refunds_paid_out": total_refunds, 
                "expected_cash_in_drawer": expected_cash,
                "actual_cash_counted": actual_cash,
                "discrepancy": round(discrepancy, 2),
                "abandoned_carts_cleared": cleared_carts
            }
        }), 200

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        
# suspend transaction 

@pos_bp.route('/pos/suspend', methods=['POST'])
@jwt_required()
def suspend_transaction():
    user_id = int(get_jwt_identity())
    claims = get_jwt()
    branch_id = claims['branch']

    data = request.json
    cart = data.get('cart')
    # Optional note to help cashier remember who the cart belongs to
    reference_note = data.get('reference_note', 'Waiting Customer') 

    if not cart or not isinstance(cart, list) or len(cart) == 0:
        return jsonify({"message": "Cannot suspend an empty cart."}), 400

    cart_json = json.dumps(cart)

    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            INSERT INTO SUSPENDED_TRANSACTIONS (branch_id, user_id, reference_note, cart_data)
            VALUES (%s, %s, %s, %s)
        """, (branch_id, user_id, reference_note, cart_json))
        
        suspend_id = cur.lastrowid
        mysql.connection.commit()
        
        return jsonify({
            "status": "success", 
            "message": "Transaction suspended successfully.",
            "suspend_id": suspend_id
        }), 201
        
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


#view suspended transactions 
@pos_bp.route('/pos/suspended-list', methods=['GET'])
@jwt_required()
def get_suspended_list():
    claims = get_jwt()
    branch_id = claims['branch']

    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            SELECT st.suspend_id, st.reference_note, st.created_at, u.username
            FROM SUSPENDED_TRANSACTIONS st
            JOIN USERS u ON st.user_id = u.user_id
            WHERE st.branch_id = %s
            ORDER BY st.created_at ASC
        """, (branch_id,))
        results = cur.fetchall()

        suspended_list = []
        for row in results:
            suspended_list.append({
                "suspend_id": row[0],
                "reference_note": row[1],
                "time_suspended": row[2].strftime('%I:%M %p'),
                "cashier": row[3]
            })
            
        return jsonify(suspended_list), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close() 

#resume transaction 

@pos_bp.route('/pos/resume/<int:suspend_id>', methods=['POST'])
@jwt_required()
def resume_transaction(suspend_id):
    claims = get_jwt()
    branch_id = claims['branch']

    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT cart_data FROM SUSPENDED_TRANSACTIONS WHERE suspend_id = %s AND branch_id = %s", (suspend_id, branch_id))
        result = cur.fetchone()

        if not result:
            return jsonify({"message": "Suspended transaction not found."}), 404

        cart_data = json.loads(result[0])

        cur.execute("DELETE FROM SUSPENDED_TRANSACTIONS WHERE suspend_id = %s", (suspend_id,))
        mysql.connection.commit()

        return jsonify({
            "status": "success",
            "message": "Transaction resumed and loaded to register.",
            "cart": cart_data
        }), 200
        
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()




# refund/return 
@pos_bp.route('/pos/refund', methods=['POST'])
@jwt_required()
def process_refund():
    cashier_id = int(get_jwt_identity())
    
    data = request.json
    
    mgr_username = data.get('manager_username')
    mgr_password = data.get('manager_password')

    if not mgr_username or not mgr_password:
        return jsonify({"message": "Manager override credentials are required to process a refund."}), 400

    sale_id = data.get('sale_id')
    sale_detail_id = data.get('sale_detail_id') 
    qty_to_return = int(data.get('quantity', 0))
    reason = data.get('reason')
    
    if not all([sale_id, sale_detail_id, qty_to_return, reason]):
        return jsonify({"message": "Missing sale_id, sale_detail_id, quantity, or reason."}), 400

    cur = mysql.connection.cursor()
    try:
        cur.execute("SELECT user_id, password_hash, role, is_active FROM USERS WHERE username = %s", (mgr_username,))
        manager = cur.fetchone()

        if not manager or not bcrypt.check_password_hash(manager[1], mgr_password):
            return jsonify({"message": "Override Failed: Invalid Manager Credentials."}), 401
        
        if manager[2] not in ['admin', 'manager']:
            return jsonify({"message": "Override Denied: User is not an admin or manager."}), 403
            
        if not manager[3]:
            return jsonify({"message": "Override Denied: Manager account is currently inactive."}), 403
            
        manager_id = manager[0] 

        cur.execute("""
            SELECT sd.inventory_id, sd.quantity_sold, sd.price_at_sale, sd.discount_applied,
                   (SELECT IFNULL(SUM(quantity_returned), 0) FROM RETURN_ITEMS WHERE sale_detail_id = %s) as already_refunded
            FROM SALES_DETAILS sd
            WHERE sd.sale_detail_id = %s AND sd.sale_id = %s
        """, (sale_detail_id, sale_detail_id, sale_id))
        
        original_item = cur.fetchone()

        if not original_item:
            return jsonify({"message": f"Verification Failed: This item was not found on Receipt #{sale_id}"}), 404
        
        inv_id, qty_sold, price, disc, already_refunded = original_item[0], original_item[1], original_item[2], original_item[3], original_item[4]
        
        remaining_refundable = qty_sold - already_refunded
        
        if qty_to_return > remaining_refundable:
            return jsonify({
                "message": f"Refund Denied: Only {remaining_refundable} units remaining for refund. (Already refunded: {already_refunded})"
            }), 400

        net_price_per_item = float(price) - (float(disc) / qty_sold if qty_sold > 0 else 0)
        total_refund_amount = net_price_per_item * qty_to_return

        cur.execute("SELECT branch_id FROM SALES_HEADERS WHERE sale_id = %s", (sale_id,))
        branch_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO SALES_RETURNS (sale_id, branch_id, user_id, return_date, total_refund_amount, return_reason)
            VALUES (%s, %s, %s, NOW(), %s, %s)
        """, (sale_id, branch_id, manager_id, total_refund_amount, reason)) 
        
        return_id = cur.lastrowid

        cur.execute("""
            INSERT INTO RETURN_ITEMS (return_id, sale_detail_id, quantity_returned, refund_amount_per_item, back_to_stock)
            VALUES (%s, %s, %s, %s, TRUE)
        """, (return_id, sale_detail_id, qty_to_return, net_price_per_item))

        cur.execute("SELECT product_id FROM BRANCH_INVENTORY WHERE inventory_id = %s", (inv_id,))
        prod_id = cur.fetchone()[0]

        cur.execute("UPDATE BRANCH_INVENTORY SET quantity_on_hand = quantity_on_hand + %s WHERE inventory_id = %s", (qty_to_return, inv_id))
        cur.execute("UPDATE PRODUCTS SET total_stock_quantity = total_stock_quantity + %s WHERE product_id = %s", (qty_to_return, prod_id))

        cur.execute("""
            INSERT INTO STOCK_ADJUSTMENTS (inventory_id, user_id, adjustment_type, quantity_adjusted, date_adjusted, remarks)
            VALUES (%s, %s, 'RETURN_RESTORE', %s, NOW(), %s)
        """, (inv_id, manager_id, qty_to_return, f"Refunded Item from Sale #{sale_id}. Authorized by {mgr_username}. Reason: {reason}"))

        mysql.connection.commit()
        
        return jsonify({
            "status": "success",
            "message": "Refund authorized and recorded.",
            "authorized_by": mgr_username,
            "data": {
                "return_id": return_id,
                "amount_returned": round(total_refund_amount, 2),
                "remaining_on_receipt": remaining_refundable - qty_to_return
            }
        }), 201

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()

#receive lookup

@pos_bp.route('/pos/transaction-lookup/<int:sale_id>', methods=['GET'])
@jwt_required()
def lookup_transaction(sale_id):
    claims = get_jwt()
    branch_id = claims['branch']

    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            SELECT sh.sale_date, sh.total_amount, u.username, sh.customer_type, sh.payment_method
            FROM SALES_HEADERS sh
            JOIN USERS u ON sh.user_id = u.user_id
            WHERE sh.sale_id = %s AND sh.branch_id = %s
        """, (sale_id, branch_id))
        
        header = cur.fetchone()

        if not header:
            return jsonify({"message": f"Receipt #{sale_id} not found in this branch."}), 404

        if header[3] == 'VOIDED':
            return jsonify({"message": f"Receipt #{sale_id} has been completely VOIDED. No refunds can be processed."}), 400

     
        cur.execute("""
            SELECT 
                sd.sale_detail_id, 
                p.product_name_official, 
                sd.quantity_sold, 
                sd.price_at_sale, 
                sd.discount_applied,
                IFNULL((SELECT SUM(quantity_returned) FROM RETURN_ITEMS WHERE sale_detail_id = sd.sale_detail_id), 0) AS already_returned
            FROM SALES_DETAILS sd
            JOIN BRANCH_INVENTORY bi ON sd.inventory_id = bi.inventory_id
            JOIN PRODUCTS p ON bi.product_id = p.product_id
            WHERE sd.sale_id = %s
        """, (sale_id,))
        
        items = cur.fetchall()

        item_list = []
        for item in items:
            detail_id, name, qty_sold, price, discount, returned = item
            refundable_qty = qty_sold - returned

            item_list.append({
                "sale_detail_id": detail_id,   
                "product_name": name,
                "original_qty_purchased": qty_sold,
                "qty_already_returned": int(returned),
                "refundable_qty_remaining": int(refundable_qty),
                "net_price_paid": float(price) - (float(discount) / qty_sold if qty_sold > 0 else 0)
            })

        return jsonify({
            "status": "success",
            "receipt_info": {
                "sale_id": sale_id,
                "date": header[0].strftime('%Y-%m-%d %H:%M:%S'),
                "total_paid": float(header[1]),
                "payment_method": header[4],
                "cashier": header[2]
            },
            "line_items": item_list
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()