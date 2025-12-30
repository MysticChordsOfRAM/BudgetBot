from datetime import date
from datetime import datetime
import psycopg2 as ps
from twilio.twiml.messaging_response import MessagingResponse
from flask import Flask, render_template, request, flash
from collections import namedtuple
import supersecrets as shh

app = Flask(__name__)

app.secret_key = shh.secret_key

OWNERNUMBER = shh.phone_number

Expense = namedtuple('Expense', ['budget', 'amount'])

class CommandProcessor():
    def __init__(self, message_body):
        self.message_body = message_body.upper()
        self.words = self.message_body.split(' ')
        self.nwords = len(self.words)

        self.root = self.words[0] if self.nwords > 0 else None
        self.branch = self.words[1] if self.nwords > 1 else None
        self.leaf = self.words[2] if self.nwords > 2 else None
        self.leaf1 = self.words[3] if self.nwords > 3 else None
        self.leaf2 = self.words[4] if self.nwords > 4 else None

        self.budget_areas = ['FOOD', 'FUN', 'OTHER']
        self.date = date.today()

        self.month = self.date.strftime('%m')
        self.year = self.date.strftime('%Y')
        self.day = self.date.strftime('%d')
        self.sql_date = f'{self.year}{self.month}{self.day}'

        self.time = datetime.now()
        self.sql_time = self.time.strftime('%H:%M')

    def dock_at_echobase(self):
        return ps.connect(host = shh.db_ip,
                          port = shh.db_port,
                          dbname = shh.db_name,
                          user = shh.db_user,
                          password = shh.db_password)
    
    def log_weight(self):

        if not self.leaf:
            return "Error: Missing Weight."
        
        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:
                    
                    cursor.execute("CREATE TABLE IF NOT EXISTS prd.weight (tpd VARCHAR(50), tod VARCHAR(50), weight DECIMAL(10, 2));")
                    insert_qry = "INSERT INTO prd.weight (tpd, tod, weight) VALUES (%s, %s, %s);"
                    cursor.execute(insert_qry, (self.sql_date, self.sql_time, self.leaf))

                    home.commit()

                    return "Weight Logged"
        
        except Exception as e:
            return f"Database Error: {e}"

        return None
    
    def log_spending(self):

        if not self.leaf:
            return "Error: Missing amount."
            
        try:
            spending = float(self.leaf) * -1
        except ValueError:
            return f"Error: '{self.leaf}' is not a valid number."

        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:
               
                    insert_qry = "INSERT INTO prd.discbudget (tpd, budget, amt, bb) VALUES (%s, %s, %s, false);"
                    cursor.execute(insert_qry, (self.sql_date, self.branch, spending))

                    home.commit()

                    select_qry = "SELECT budget, amt FROM prd.discbudget WHERE budget = %s"
                    cursor.execute(select_qry, (self.branch,))

                    rows = cursor.fetchall()
                    values = [Expense(row[0], row[1]) for row in rows]
                    newbalance = round(sum([i.amount for i in values]), 2)

                    return f"Spent ${abs(spending)} on {self.branch}. New Balance: ${newbalance}"
        
        except Exception as e:
            return f"Database Error in log_spending: {e}"
        
    def balance_check(self):
        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:

                    cursor.execute("SELECT budget, amt FROM prd.discbudget")
                    rows = cursor.fetchall()

                    values = [Expense(row[0], row[1]) for row in rows]

                    food = round(sum([i.amount for i in values if i.budget == 'FOOD']), 2)
                    fun = round(sum([i.amount for i in values if i.budget == 'FUN']), 2)
                    other = round(sum([i.amount for i in values if i.budget == 'OTHER']), 2)
                    total = round(food + fun + other, 2)

                    return f"FOOD   {food}\nFUN      {fun}\nOTHER  {other}\nTOTAL  {total}"
        
        except Exception as e:
            return f"Error given: {e}"
        

    def top_up(self):

        if not self.leaf and not self.leaf1 and not self.leaf2:
            return "Error: Missing amount."
            
        try:
            food = float(self.leaf)
            fun = float(self.leaf1)
            other = float(self.leaf2)
        except ValueError:
            return "Invalid Leaf"

        topups = [food, fun, other]

        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:

                    for ii in range(0, 3):
                        cat = self.budget_areas[ii]
                        topper = topups[ii]
                        insert_qry = "INSERT INTO prd.discbudget (tpd, budget, amt, bb) VALUES (%s, %s, %s, false);"
                        cursor.execute(insert_qry, (self.sql_date, cat, topper))

                    home.commit()

                    cursor.execute("SELECT budget, amt FROM prd.discbudget")
                    rows = cursor.fetchall()

                    values = [Expense(row[0], row[1]) for row in rows]

                    food = round(sum([i.amount for i in values if i.budget == 'FOOD']), 2)
                    fun = round(sum([i.amount for i in values if i.budget == 'FUN']), 2)
                    other = round(sum([i.amount for i in values if i.budget == 'OTHER']), 2)
                    total = round(food + fun + other, 2)

                    return f"FOOD   {food}\nFUN      {fun}\nOTHER  {other}\nTOTAL  {total}"
 
        except Exception as e:
            return f"Error given: {e}"
        
    def help(self):
        help_menu = """
BUDGET (FOOD, FUN, OTHER) (amount)
BUDGET BALANCE
BUDGET TOPUP [FOOD] [FUN] [OTHER]
WEIGHT LOG (weight)
        """
        
        return help_menu
        
    def process(self):
        
        if self.root == 'BUDGET':
            if self.branch in self.budget_areas:
                return self.log_spending()
            elif self.branch == 'BALANCE':
                return self.balance_check()
            elif self.branch == 'TOPUP':
                return self.top_up()
            else:
                return 'Not valid branch command for root BUDGET'
                     
        elif self.root == 'GUIDE':
            return self.help()
        
        elif self.root == 'WEIGHT':
            if self.branch == 'LOG':
                return self.log_weight()
                
        else:
            return 'Not valid root command'
               
@app.route("/sms", methods = ['POST'])
def COSMO():

    sender_number = request.values.get('From')
    message_body = request.values.get('Body', '').strip()

    resp = MessagingResponse()

    if sender_number == OWNERNUMBER:

        processor = CommandProcessor(message_body)

        reply_text = processor.process()

        resp.message(reply_text)

    return str(resp)
    
@app.route('/', methods=['GET', 'POST'])
def home():
    if request.method == 'POST':
        
        phone_input = request.form.get('phone')
        
        flash("ACCESS DENIED: This phone number is not on the authorized administrator list. No data was saved.")
        
        return render_template('index.html')

    return render_template('index.html')

@app.route('/about')
def about_us():
    return render_template('about_us.html')

@app.route('/terms')
def terms_of_service():
    return render_template('terms_of_service.html')

@app.route('/privacy')
def privacy_policy():
    return render_template('privacy_policy.html')

if __name__ == "__main__":
    app.run(host = shh.app_host, port = shh.app_port)
