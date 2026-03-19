from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from extensions import mysql

# Define the blueprint
branch_bp = Blueprint('branch',__name__)

# CREATE NEW BRANCH
@branch_bp.route('/create-branch', methods=['POST'])
@jwt_required()
def create_branch():
    """
    Create a new pharmacy branch.
    Only admins can create branches.
    """
    claims = get_jwt()
    current_role = claims.get('role')

    # Only admins can create branches
    if current_role != 'admin':
        return jsonify({"message": "Access Denied: Only administrators can create new branches."}), 403

    data = request.json
    branch_id = data.get('branch_id')
    branch_name = data.get('branch_name')
    branch_code = data.get('branch_code')

    # Validation
    if not all([branch_id, branch_name, branch_code]):
        return jsonify({
            "message": "Validation Error: All fields (branch_id, branch_name, branch_code) are required."
        }), 400

    cur = mysql.connection.cursor()
    try:
        # Check for duplicate branch_id
        cur.execute("SELECT branch_id FROM BRANCHES WHERE branch_id = %s", (branch_id,))
        if cur.fetchone():
            return jsonify({"message": f"Conflict: Branch ID {branch_id} already exists."}), 409

        # Check for duplicate branch_code
        cur.execute("SELECT branch_code FROM BRANCHES WHERE branch_code = %s", (branch_code,))
        if cur.fetchone():
            return jsonify({"message": f"Conflict: Branch code '{branch_code}' is already in use."}), 409

        # Check for duplicate branch_name
        cur.execute("SELECT branch_name FROM BRANCHES WHERE branch_name = %s", (branch_name,))
        if cur.fetchone():
            return jsonify({"message": f"Conflict: Branch name '{branch_name}' already exists."}), 409

        # Insert new branch
        cur.execute("""
            INSERT INTO BRANCHES (branch_id, branch_name, branch_code)
            VALUES (%s, %s, %s)
        """, (branch_id, branch_name, branch_code))
        
        mysql.connection.commit()
        
        return jsonify({
            "message": f"Branch '{branch_name}' created successfully!",
            "branch": {
                "branch_id": branch_id,
                "branch_name": branch_name,
                "branch_code": branch_code
            }
        }), 201

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# GET ALL BRANCHES
@branch_bp.route('/branches', methods=['GET'])
@jwt_required()
def get_all_branches():
    """
    Retrieve all branches.
    Available to all authenticated users.
    """
    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            SELECT branch_id, branch_name, branch_code 
            FROM BRANCHES 
            ORDER BY branch_id
        """)
        
        branches = cur.fetchall()
        
        branch_list = []
        for branch in branches:
            branch_list.append({
                "branch_id": branch[0],
                "branch_name": branch[1],
                "branch_code": branch[2]
            })

        return jsonify({
            "status": "success",
            "count": len(branch_list),
            "branches": branch_list
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# GET SINGLE BRANCH BY ID
@branch_bp.route('/branches/<int:branch_id>', methods=['GET'])
@jwt_required()
def get_branch_by_id(branch_id):
    """
    Get details of a specific branch.
    """
    cur = mysql.connection.cursor()
    try:
        cur.execute("""
            SELECT branch_id, branch_name, branch_code 
            FROM BRANCHES 
            WHERE branch_id = %s
        """, (branch_id,))
        
        branch = cur.fetchone()
        
        if not branch:
            return jsonify({"message": f"Branch ID {branch_id} not found."}), 404
            
        return jsonify({
            "status": "success",
            "branch": {
                "branch_id": branch[0],
                "branch_name": branch[1],
                "branch_code": branch[2]
            }
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# UPDATE BRANCH
@branch_bp.route('/update-branch/<int:branch_id>', methods=['PUT'])
@jwt_required()
def update_branch(branch_id):
    """
    Update branch information.
    Only admins can update branches.
    """
    claims = get_jwt()
    current_role = claims.get('role')

    # Only admins can update branches
    if current_role != 'admin':
        return jsonify({"message": "Access Denied: Only administrators can update branch information."}), 403

    data = request.json
    if not data:
        return jsonify({"message": "No data provided to update."}), 400

    branch_name = data.get('branch_name')
    branch_code = data.get('branch_code')

    cur = mysql.connection.cursor()
    try:
        # Verify branch exists
        cur.execute("SELECT * FROM BRANCHES WHERE branch_id = %s", (branch_id,))
        if not cur.fetchone():
            return jsonify({"message": f"Branch ID {branch_id} not found."}), 404

        # Check for duplicate branch_name (excluding current branch)
        if branch_name:
            cur.execute("""
                SELECT branch_id FROM BRANCHES 
                WHERE branch_name = %s AND branch_id != %s
            """, (branch_name, branch_id))
            if cur.fetchone():
                return jsonify({"message": f"Conflict: Branch name '{branch_name}' is already in use."}), 409

        # Check for duplicate branch_code (excluding current branch)
        if branch_code:
            cur.execute("""
                SELECT branch_id FROM BRANCHES 
                WHERE branch_code = %s AND branch_id != %s
            """, (branch_code, branch_id))
            if cur.fetchone():
                return jsonify({"message": f"Conflict: Branch code '{branch_code}' is already in use."}), 409

        # Build update query dynamically
        update_fields = []
        update_values = []

        if branch_name:
            update_fields.append("branch_name = %s")
            update_values.append(branch_name)
        
        if branch_code:
            update_fields.append("branch_code = %s")
            update_values.append(branch_code)

        if not update_fields:
            return jsonify({"message": "No valid fields provided to update."}), 400

        # Add branch_id to the end of values list
        update_values.append(branch_id)

        # Execute update
        sql = f"UPDATE BRANCHES SET {', '.join(update_fields)} WHERE branch_id = %s"
        cur.execute(sql, tuple(update_values))
        mysql.connection.commit()

        return jsonify({
            "message": f"Branch ID {branch_id} updated successfully!",
            "updated_fields": {k: v for k, v in data.items() if v}
        }), 200

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()


# DELETE BRANCH (Optional - with safety checks)
@branch_bp.route('/delete-branch/<int:branch_id>', methods=['DELETE'])
@jwt_required()
def delete_branch(branch_id):
    """
    Delete a branch (with safety checks).
    Only admins can delete branches.
    WARNING: This checks for dependencies before deleting.
    """
    claims = get_jwt()
    current_role = claims.get('role')

    if current_role != 'admin':
        return jsonify({"message": "Access Denied: Only administrators can delete branches."}), 403

    cur = mysql.connection.cursor()
    try:
        # Check if branch exists
        cur.execute("SELECT branch_name FROM BRANCHES WHERE branch_id = %s", (branch_id,))
        branch = cur.fetchone()
        
        if not branch:
            return jsonify({"message": f"Branch ID {branch_id} not found."}), 404

        # Safety check: Check if branch has users
        cur.execute("SELECT COUNT(*) FROM USERS WHERE branch_id = %s", (branch_id,))
        user_count = cur.fetchone()[0]
        
        if user_count > 0:
            return jsonify({
                "message": f"Cannot delete branch: {user_count} user(s) are assigned to this branch. Reassign them first."
            }), 409

        # Safety check: Check if branch has inventory
        cur.execute("SELECT COUNT(*) FROM BRANCH_INVENTORY WHERE branch_id = %s", (branch_id,))
        inventory_count = cur.fetchone()[0]
        
        if inventory_count > 0:
            return jsonify({
                "message": f"Cannot delete branch: Branch has {inventory_count} inventory record(s). Clear inventory first."
            }), 409

        # Safety check: Check if branch has gondolas
        cur.execute("SELECT COUNT(*) FROM GONDOLAS WHERE branch_id = %s", (branch_id,))
        gondola_count = cur.fetchone()[0]
        
        if gondola_count > 0:
            return jsonify({
                "message": f"Cannot delete branch: Branch has {gondola_count} gondola(s). Remove them first."
            }), 409

        # If all checks pass, delete the branch
        cur.execute("DELETE FROM BRANCHES WHERE branch_id = %s", (branch_id,))
        mysql.connection.commit()

        return jsonify({
            "message": f"Branch '{branch[0]}' (ID: {branch_id}) deleted successfully."
        }), 200

    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()