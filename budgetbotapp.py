from datetime import date
from datetime import datetime, timezone
import psycopg2 as ps
from twilio.twiml.messaging_response import MessagingResponse
from flask import Flask, render_template, request, flash, Response
from collections import namedtuple
from prometheus_flask_exporter import PrometheusMetrics
import supersecrets as shh

app = Flask(__name__)
metrics = PrometheusMetrics(app, group_by = 'endpoint')

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

    def calculate_tdee(self, weight_lbs, height_cm=180, age=32, is_male=True, activity_multiplier=1.2):
        weight_kg = weight_lbs / 2.20462
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age)
        bmr += 5 if is_male else -161
        return int(bmr * activity_multiplier)

    def get_daily_intake(self, cursor):
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM prd.calorie_ledger
            WHERE timestamp AT TIME ZONE 'America/New_York' >= date_trunc('day', CURRENT_TIMESTAMP AT TIME ZONE 'America/New_York')
            """)
        return int(cursor.fetchone()[0])

    def get_calorie_balance(self, home, cursor, mid_check = False):

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS prd.calorie_settings (
                id SERIAL PRIMARY KEY,
                daily_burn_rate INT,
                anchor_timestamp TIMESTAMPTZ,
                anchor_balance NUMERIC(10, 2)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS prd.calorie_ledger (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ,
                amount INT
            );
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_calorie_ledger_timestamp ON prd.calorie_ledger(timestamp);")

        cursor.execute("""
            SELECT daily_burn_rate, anchor_timestamp, anchor_balance 
            FROM prd.calorie_settings 
            ORDER BY id DESC LIMIT 1
        """)
        settings = cursor.fetchone()

        if not settings:
            cursor.execute("""
                INSERT INTO prd.calorie_settings (daily_burn_rate, anchor_timestamp, anchor_balance)
                VALUES (2400, CURRENT_TIMESTAMP, 0) RETURNING daily_burn_rate, anchor_timestamp, anchor_balance
            """)
            settings = cursor.fetchone()

        anchor_time = settings[1]
        anchor_balance = settings[2]
        daily_rate = settings[0]

        now = datetime.now(timezone.utc)
        seconds_elapsed = (now - anchor_time).total_seconds()
        calories_per_second = daily_rate / 86400
        accrued_calories = seconds_elapsed * calories_per_second

        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM prd.calorie_ledger
            WHERE timestamp >= %s
        """, (anchor_time,))
        deductions = cursor.fetchone()[0]

        current_balance = float(anchor_balance) + accrued_calories - float(deductions)

        max_allowance = daily_rate

        if current_balance > max_allowance:
            current_balance = max_allowance

            if not mid_check:
                cursor.execute("""
                    INSERT INTO prd.calorie_settings (daily_burn_rate, anchor_timestamp, anchor_balance)
                    VALUES (%s, CURRENT_TIMESTAMP, %s)
                """, (daily_rate, current_balance))
                home.commit()

        return current_balance, daily_rate

    def get_caloric_reset_time(self, cursor, current_balance, daily_rate):

        if current_balance >= daily_rate:
            return "At Cap"

        calories_needed = daily_rate - current_balance
        calories_per_second = daily_rate / 86400
        seconds_to_full = calories_needed / calories_per_second

        cursor.execute("""
            SELECT
                (NOW() + interval '1 second' * %s) AT TIME ZONE 'America/New_York',
                (NOW() AT TIME ZONE 'America/New_York')::date
            """, (seconds_to_full,))
        full_time, current_date = cursor.fetchone()

        time_str = full_time.strftime('%I:%M %p').lstrip('0')

        if full_time.date() > current_date:
            return f"{time_str} (Tomorrow)"

        return time_str

    def log_weight(self):
        if not self.leaf:
            return "Error: Missing Weight."

        try:
            weight_lbs = float(self.leaf)
        except ValueError:
            return f"Error: '{self.leaf}' is not a valid number."

        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:
                    cursor.execute("CREATE TABLE IF NOT EXISTS prd.weight (tpd VARCHAR(50), tod VARCHAR(50), weight DECIMAL(10, 2));")
                    insert_qry = "INSERT INTO prd.weight (tpd, tod, weight) VALUES (%s, %s, %s);"
                    cursor.execute(insert_qry, (self.sql_date, self.sql_time, weight_lbs))

                    current_balance, _ = self.get_calorie_balance(home, cursor, mid_check = True)
                    new_tdee = self.calculate_tdee(weight_lbs)

                    cursor.execute("""
                        INSERT INTO prd.calorie_settings (daily_burn_rate, anchor_timestamp, anchor_balance)
                        VALUES (%s, CURRENT_TIMESTAMP, %s)
                    """, (new_tdee, current_balance))

                    home.commit()
                    return f"Weight Logged. New TDEE set to {new_tdee} kcal/day."

        except Exception as e:
            return f"Database Error: {e}"

    def log_calorie_intake(self):
        if not self.leaf:
            return "Error: Missing amount."

        try:
            calories = int(self.leaf)
        except ValueError:
            return f"Error: '{self.leaf}' is not a valid integer."

        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:
                    self.get_calorie_balance(home, cursor)

                    cursor.execute("""
                        INSERT INTO prd.calorie_ledger (timestamp, amount)
                        VALUES (CURRENT_TIMESTAMP, %s)
                    """, (calories,))

                    home.commit()

                    new_balance, daily_rate = self.get_calorie_balance(home, cursor)
                    full_at = self.get_caloric_reset_time(cursor, new_balance, daily_rate)
                    daily_total = self.get_daily_intake(cursor)

                    return f"Logged {calories} kcal\nBalance: {int(new_balance)} kcal\nFull At: {full_at}\nIntake: {daily_total} kcal"
        except Exception as e:
            return f"Database Error in log_calorie_intake: {e}"

    def check_calorie_balance(self):
        try:
            with self.dock_at_echobase() as home:
                with home.cursor() as cursor:
                    current_balance, daily_rate = self.get_calorie_balance(home, cursor)
                    full_at = self.get_caloric_reset_time(cursor, current_balance, daily_rate)
                    daily_total = self.get_daily_intake(cursor)

                    return f"Current budget: {int(current_balance)} kcal\nFull At: {full_at}\nTodays Total: {daily_total}"
        except Exception as e:
            return f"Database Error: {e}"

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
CALORIE LOG (amount)
CALORIE BALANCE
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
            
        elif self.root == 'CALORIE':
            if self.branch == 'LOG':
                return self.log_calorie_intake()
            elif self.branch == 'BALANCE':
                return self.check_calorie_balance()
            else:
                return 'Not valid branch command for root CALORIE'
                     
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
