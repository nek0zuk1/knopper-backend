import pymysql
pymysql.install_as_MySQLdb()
import os
from flask import Flask
from datetime import timedelta
from dotenv import load_dotenv
from extensions import mysql, bcrypt, jwt
from user import user_bp
from inventory import inventory_bp
from pos import pos_bp
from procurement import procurement_bp
from flask_cors import CORS
load_dotenv()

app = Flask(__name__)
CORS(app)
app.config['MYSQL_HOST'] = os.getenv('DB_HOST')
app.config['MYSQL_USER'] = os.getenv('DB_USER')
app.config['MYSQL_PASSWORD'] = os.getenv('DB_PASSWORD') 
app.config['MYSQL_DB'] = os.getenv('DB_NAME')
app.config['MYSQL_PORT'] = int(os.getenv('DB_PORT'))
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET') 
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=12)


mysql.init_app(app)
bcrypt.init_app(app)
jwt.init_app(app)

app.register_blueprint(user_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(pos_bp)
app.register_blueprint(procurement_bp)

if __name__ == '__main__':
    app.run(debug=True)