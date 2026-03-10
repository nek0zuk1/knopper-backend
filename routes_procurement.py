from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from extensions import mysql

procurement_bp = Blueprint('procurement', __name__)


def next_id(cursor, table, id_col):
    """Auto-generate the next ID for any table."""
    cursor.execute(f"SELECT IFNULL(MAX({id_col}), 0) + 1 FROM {table}")
    return cursor.fetchone()[0]


# GET /purchase-orders
@procurement_bp.route('/procurement', methods=['GET'])
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
                   creator.full_name, approver.full_name, MAX(rr.date_received), po.date_cancelled
            FROM PURCHASE_ORDERS po
            LEFT JOIN SUPPLIERS s ON po.supplier_id = s.supplier_id
            LEFT JOIN BRANCHES b ON po.branch_id = b.branch_id
            LEFT JOIN USERS creator ON po.created_by_user_id = creator.user_id
            LEFT JOIN USERS approver ON po.approved_by_user_id = approver.user_id
            LEFT JOIN RECEIVING_REPORTS rr ON po.order_id = rr.order_id
            GROUP BY po.order_id, po.order_date, po.status, po.total_amount,
                     s.supplier_name, b.branch_name, creator.full_name, approver.full_name, po.date_cancelled
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
            "approved_by": r[7],
            "date_received": r[8].strftime('%Y-%m-%d %H:%M') if r[8] else None,
            "date_cancelled": r[9].strftime('%Y-%m-%d %H:%M') if r[9] else None
        } for r in rows]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()



# GET /procurement/<id> - Get PO details with items
@procurement_bp.route('/procurement/<int:order_id>', methods=['GET'])
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
                   creator.full_name, approver.full_name, rr.date_received, po.date_cancelled
            FROM PURCHASE_ORDERS po
            LEFT JOIN SUPPLIERS s ON po.supplier_id = s.supplier_id
            LEFT JOIN BRANCHES b ON po.branch_id = b.branch_id
            LEFT JOIN USERS creator ON po.created_by_user_id = creator.user_id
            LEFT JOIN USERS approver ON po.approved_by_user_id = approver.user_id
            LEFT JOIN RECEIVING_REPORTS rr ON po.order_id = rr.order_id
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
            'date_received': po[8].strftime('%Y-%m-%d %H:%M') if po[8] else None,
            'date_cancelled': po[9].strftime('%Y-%m-%d %H:%M') if po[9] else None,
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

# POST /purchase-orders
# Send: supplier_id, branch_id, items[{product_id, quantity, cost}]
@procurement_bp.route('/procurement', methods=['POST'])
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
            (order_id, supplier_id, branch_id, created_by_user_id, approved_by_user_id, order_date, status, total_amount)
            VALUES (%s, %s, %s, %s, %s, NOW(), 'DRAFT', %s)
        """, (order_id, supplier_id, branch_id, current_user_id, current_user_id, total))
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
@procurement_bp.route('/procurement/<int:order_id>', methods=['PUT'])
@jwt_required()
def update_purchase_order(order_id):
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    data       = request.json
    new_status = data.get('status')
    if not new_status:
        return jsonify({"message": "Need: status (DRAFT / APPROVED / SENT / RECEIVED / CANCELLED)"}), 400
    cur = mysql.connection.cursor()
    try:
        if new_status == 'CANCELLED':
            cur.execute("UPDATE PURCHASE_ORDERS SET status=%s, approved_by_user_id=%s, date_cancelled=NOW() WHERE order_id=%s",
                        (new_status, current_user_id, order_id))
        else:
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
@procurement_bp.route('/procurement/receive', methods=['POST'])
@jwt_required()
def receive_delivery():
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    data     = request.json
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({"message": "Need: order_id"}), 400
    cur = mysql.connection.cursor()
    try:
        # Check PO status first
        cur.execute("SELECT status FROM PURCHASE_ORDERS WHERE order_id=%s", (order_id,))
        po = cur.fetchone()
        if not po:
            return jsonify({"message": "PO not found"}), 404
        if po[0] == 'CANCELLED':
            return jsonify({"message": "Cannot receive a CANCELLED PO!"}), 400
        if po[0] == 'RECEIVED':
            return jsonify({"message": "This PO has already been received!"}), 400

        # Auto-fetch all items from the PO including product_id and branch_id
        cur.execute("""
            SELECT poi.po_item_id, poi.quantity_ordered, poi.product_id, po.branch_id
            FROM PURCHASE_ORDER_ITEMS poi
            JOIN PURCHASE_ORDERS po ON poi.order_id = po.order_id
            WHERE poi.order_id = %s
        """, (order_id,))
        items = cur.fetchall()
        if not items:
            return jsonify({"message": "No items found for this PO"}), 404

        receipt_id = next_id(cur, 'RECEIVING_REPORTS', 'receipt_id')
        cur.execute("""
            INSERT INTO RECEIVING_REPORTS
            (receipt_id, order_id, received_by_user_id, date_received)
            VALUES (%s, %s, %s, NOW())
        """, (receipt_id, order_id, current_user_id))
        for item in items:
            po_item_id, quantity, product_id, branch_id = item

            # Pull batch and expiry from BRANCH_INVENTORY
            cur.execute("""
                SELECT batch_number, expiry_date
                FROM BRANCH_INVENTORY
                WHERE product_id = %s AND branch_id = %s
                LIMIT 1
            """, (product_id, branch_id))
            inv = cur.fetchone()
            batch  = inv[0] if inv and inv[0] else None
            expiry = inv[1] if inv and inv[1] else None

            receipt_item_id = next_id(cur, 'RECEIPT_ITEMS', 'receipt_item_id')
            cur.execute("""
                INSERT INTO RECEIPT_ITEMS
                (receipt_item_id, receipt_id, po_item_id, quantity_received, batch_number, expiry_date)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (receipt_item_id, receipt_id, po_item_id, quantity, batch, expiry))
            cur.execute("UPDATE PURCHASE_ORDER_ITEMS SET item_status='RECEIVED' WHERE po_item_id=%s", (po_item_id,))
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
@procurement_bp.route('/procurement/transfer', methods=['POST'])
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
            product_id = item.get('product_id')
            quantity   = item.get('quantity')

            # Auto-pull batch and expiry from BRANCH_INVENTORY
            cur.execute("""
                SELECT batch_number, expiry_date
                FROM BRANCH_INVENTORY
                WHERE product_id = %s AND branch_id = %s
                LIMIT 1
            """, (product_id, from_branch))
            inv = cur.fetchone()
            batch  = inv[0] if inv and inv[0] else None
            expiry = inv[1] if inv and inv[1] else None

            transfer_item_id = next_id(cur, 'TRANSFER_ITEMS', 'transfer_item_id')
            cur.execute("""
                INSERT INTO TRANSFER_ITEMS
                (transfer_item_id, manifest_id, product_id, batch_number, quantity_sent, quantity_received)
                VALUES (%s, %s, %s, %s, %s, 0)
            """, (transfer_item_id, manifest_id, product_id, batch, quantity))
            cur.execute("""
                UPDATE BRANCH_INVENTORY
                SET quantity_on_hand = quantity_on_hand - %s
                WHERE product_id=%s AND branch_id=%s AND batch_number=%s
            """, (quantity, product_id, from_branch, batch))
        mysql.connection.commit()
        return jsonify({"message": "Transfer created!", "manifest_id": manifest_id}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()




# PUT /procurement/transfer/<manifest_id>/deliver
# Confirms delivery of a transfer and updates stock in the receiving branch
@procurement_bp.route('/procurement/transfer/<int:manifest_id>/deliver', methods=['PUT'])
@jwt_required()
def confirm_transfer_delivery(manifest_id):
    claims = get_jwt()
    current_user_id = int(get_jwt_identity())
    if claims['role'] not in ['admin', 'manager']:
        return jsonify({"message": "Access Denied"}), 403
    cur = mysql.connection.cursor()
    try:
        # Check manifest exists and is still IN_TRANSIT
        cur.execute("SELECT status, to_branch_id FROM TRANSFER_MANIFEST WHERE manifest_id=%s", (manifest_id,))
        manifest = cur.fetchone()
        if not manifest:
            return jsonify({"message": "Transfer manifest not found"}), 404
        if manifest[0] == 'DELIVERED':
            return jsonify({"message": "This transfer has already been delivered!"}), 400
        if manifest[0] != 'IN_TRANSIT':
            return jsonify({"message": "Only IN_TRANSIT transfers can be confirmed"}), 400

        to_branch = manifest[1]

        # Fetch all transfer items
        cur.execute("""
            SELECT product_id, batch_number, quantity_sent
            FROM TRANSFER_ITEMS
            WHERE manifest_id = %s
        """, (manifest_id,))
        items = cur.fetchall()
        if not items:
            return jsonify({"message": "No items found for this transfer"}), 404

        for item in items:
            product_id, batch_number, quantity_sent = item

            # Update quantity_received in TRANSFER_ITEMS
            cur.execute("""
                UPDATE TRANSFER_ITEMS
                SET quantity_received = quantity_sent
                WHERE manifest_id = %s AND product_id = %s
            """, (manifest_id, product_id))

            # Add stock to receiving branch inventory
            cur.execute("""
                SELECT inventory_id FROM BRANCH_INVENTORY
                WHERE branch_id = %s AND product_id = %s AND batch_number = %s
            """, (to_branch, product_id, batch_number))
            existing = cur.fetchone()

            if existing:
                cur.execute("""
                    UPDATE BRANCH_INVENTORY
                    SET quantity_on_hand = quantity_on_hand + %s
                    WHERE inventory_id = %s
                """, (quantity_sent, existing[0]))
            else:
                inventory_id = next_id(cur, 'BRANCH_INVENTORY', 'inventory_id')
                cur.execute("""
                    INSERT INTO BRANCH_INVENTORY
                    (inventory_id, branch_id, product_id, batch_number, quantity_on_hand)
                    VALUES (%s, %s, %s, %s, %s)
                """, (inventory_id, to_branch, product_id, batch_number, quantity_sent))

        # Mark manifest as DELIVERED
        cur.execute("""
            UPDATE TRANSFER_MANIFEST
            SET status = 'DELIVERED', date_arrived = NOW()
            WHERE manifest_id = %s
        """, (manifest_id,))

        mysql.connection.commit()
        return jsonify({"message": f"Transfer {manifest_id} marked as DELIVERED!"}), 200
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()