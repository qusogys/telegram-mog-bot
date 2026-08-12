import os, re, sqlite3, secrets, threading
from time import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN=os.getenv("BOT_TOKEN")
OWNER_ID=int(os.getenv("OWNER_ID","0"))
DB_PATH=os.getenv("DB_PATH","economy.db")
PORT=int(os.getenv("PORT","10000"))
START_BALANCE=10_000
BONUS_AMOUNT=5_000
BONUS_COOLDOWN=4*60*60
RED={1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
history=[]
mines_games={}
LOCK=threading.RLock()

db=sqlite3.connect(DB_PATH,check_same_thread=False)
db.row_factory=sqlite3.Row
with LOCK:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS users(
      user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
      balance INTEGER NOT NULL DEFAULT 10000, last_bonus INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS promo_codes(
      code TEXT PRIMARY KEY,max_uses INTEGER NOT NULL,used_count INTEGER NOT NULL DEFAULT 0,
      amount INTEGER NOT NULL,created_at INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS promo_uses(
      code TEXT NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(code,user_id))""")
    db.commit()

def now(): return int(time())
def money(n): return f"{int(n):,}".replace(","," ")
def amount(s):
    s=str(s).lower().replace(" ","").replace(",",".")
    m=re.fullmatch(r"(\d+(?:\.\d+)?)(кк|kk|к|k|млн|м)?",s)
    if not m: raise ValueError
    mult={"":1,"к":1000,"k":1000,"кк":1000000,"kk":1000000,"м":1000000,"млн":1000000}
    n=int(float(m.group(1))*mult[m.group(2) or ""])
    if n<=0: raise ValueError
    return n

def ensure(u):
    with LOCK:
        db.execute("""INSERT INTO users(user_id,username,first_name,balance)
        VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,first_name=excluded.first_name""",
        (u.id,u.username,u.first_name,START_BALANCE))
        db.commit()

def balance(uid):
    with LOCK:
        r=db.execute("SELECT balance FROM users WHERE user_id=?",(uid,)).fetchone()
        return int(r["balance"]) if r else 0

def debit(uid,n):
    if n<=0:return False
    with LOCK:
        c=db.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",(n,uid,n))
        db.commit(); return c.rowcount==1

def credit(uid,n):
    with LOCK:
        c=db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(n,uid))
        db.commit(); return c.rowcount==1

def transfer(a,b,n):
    if a==b or n<=0:return False
    with LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")
            c=db.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",(n,a,n))
            if c.rowcount!=1: db.rollback(); return False
            c=db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(n,b))
            if c.rowcount!=1: db.rollback(); return False
            db.commit(); return True
        except: db.rollback(); return False

def bet(s):
    s=s.strip().lower()
    if s in {"ақ","а","ак","white"}:return ("color","white")
    if s in {"қара","қ","кара","black","черный"}:return ("color","black")
    if s in {"жұп","чет","even"}:return ("parity","even")
    if s in {"тақ","так","нечет","odd"}:return ("parity","odd")
    if re.fullmatch(r"\d+",s):
        n=int(s)
        if 0<=n<=36:return ("number",n)
    m=re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)",s)
    if m:
        a,b=map(int,m.groups())
        if 0<=a<=b<=36:return ("range",(a,b))
    return None

def mult(k,v):
    if k=="number":return 100 if v==0 else 36
    if k in ("color","parity"):return 2
    if k=="range":return max(1.01,min(35,(36/(v[1]-v[0]+1))*0.95))
    return 1

def win(k,v,r):
    if k=="number":return r==v
    if k=="range":return v[0]<=r<=v[1]
    if k=="color":return r!=0 and (("white" if r in RED else "black")==v)
    if k=="parity":return r!=0 and (("even" if r%2==0 else "odd")==v)
    return False

def bname(k,v):
    if k=="number":return str(v)
    if k=="range":return f"{v[0]}-{v[1]}"
    if k=="color":return "ақ" if v=="white" else "қара"
    return "жұп" if v=="even" else "тақ"

def rtext(n):
    return "🟢 0" if n==0 else (f"⚪ {n}" if n in RED else f"⚫ {n}")

def hist():
    return "📜 СОҢҒЫ НӘТИЖЕЛЕР\n\n"+"  ".join(rtext(x) for x in history) if history else "📜 Әзірге нәтиже жоқ."

def mine_mult(safe,count):
    if safe<=0:return 1.0
    total=25-count; x=1
    for step in range(1,safe+1): x*=((26-step)/(total-step+1))*0.97
    return max(1.01,x)

def board(g,reveal=False):
    bs=[]
    for i in range(25):
        if reveal:t="💣" if i in g["mines"] else ("💎" if i in g["opened"] else "🟩")
        else:t="💎" if i in g["opened"] else "⬜"
        bs.append(InlineKeyboardButton(t,callback_data=f"mine:{g['user_id']}:{i}"))
    rows=[bs[i:i+5] for i in range(0,25,5)]
    if not reveal:rows.append([InlineKeyboardButton("💰 Ұтысты алу",callback_data=f"cash:{g['user_id']}")])
    return InlineKeyboardMarkup(rows)

async def start(u,c):
    ensure(u.effective_user)
    await u.message.reply_text(f"🇰🇿 Сәлем!\n\n💰 Бастапқы баланс: {money(START_BALANCE)} ₸\n🎁 Бонус: {money(BONUS_AMOUNT)} ₸ / 4 сағат\n\nБаланс: баланс / б / бал\nБонус: бонус / bonus\nМины: мины 1000 [мин]\nРулетка: 2000 ақ / 2000 16 / 2000 16-30\nАударым: reply → бер 5к\n/help — көмек")

async def bal(u,c):
    ensure(u.effective_user); await u.message.reply_text(f"💰 Баланс: {money(balance(u.effective_user.id))} ₸")

async def bonus(u,c):
    x=u.effective_user; ensure(x)
    with LOCK:
        r=db.execute("SELECT last_bonus FROM users WHERE user_id=?",(x.id,)).fetchone()
        left=BONUS_COOLDOWN-(now()-r["last_bonus"])
        if left>0:
            await u.message.reply_text(f"⏳ Қалғаны: {left//3600} сағ {(left%3600)//60} мин."); return
        db.execute("UPDATE users SET balance=balance+?,last_bonus=? WHERE user_id=?",(BONUS_AMOUNT,now(),x.id)); db.commit()
    await u.message.reply_text(f"🎁 +{money(BONUS_AMOUNT)} ₸\n⏰ Келесі бонус 4 сағаттан кейін.")

async def helpcmd(u,c):
    await u.message.reply_text("🇰🇿 КӨМЕК\n\nБаланс: баланс / б / бал / ақша\nБонус: бонус / bonus — әр 4 сағат\n\nРулетка: 2000 ақ/а, 2000 қара/қ, 2000 16, 2000 16-30, 2000 жұп/тақ\n0 = 🟢 100x, нақты сан = 36x.\n\nМины: мины 1000 или мины 1000 5. Әдепкісі — 5 мина.\n\nАударым: reply жасап «бер 5к», @username немесе ID.\nПромо: /promo КОД\nИесі: /createp код саны сома\nТарих: тарих / /history\n\n3к=3000, 30к=30000, 3кк=3000000")

async def roulette(u,a,b):
    x=u.effective_user; ensure(x)
    try:n=amount(a)
    except:await u.message.reply_text("❌ Сома қате.");return
    z=bet(b)
    if not z:await u.message.reply_text("❌ Ставка түсініксіз.");return
    k,v=z
    if k=="number" and v==0 and n>1000:await u.message.reply_text("❌ 0 санына ең көбі 1000 ₸.");return
    if not debit(x.id,n):
        await u.message.reply_text(f"❌ Қаражат жеткіліксіз.\nҚолжетімді: {money(balance(x.id))} ₸");return
    r=secrets.randbelow(37);history.append(r)
    if len(history)>20:del history[:-20]
    m=mult(k,v)
    if win(k,v,r):
        p=int(n*m);credit(x.id,p); out=f"🎉 ҰТЫС! {m:.2f}x\n+{money(p)} ₸"
    else:out="❌ Ұтылдың."
    await u.message.reply_text(f"🎰 {rtext(r)}\n\n{out}\nСтавка: {money(n)} ₸\nТаңдау: {bname(k,v)}\n\n{hist()}")

async def mines_start(u,parts):
    try:n=amount(parts[1]); count=int(parts[2]) if len(parts)>2 else 5
    except:await u.message.reply_text("❌ Формат: мины 1000 или мины 1000 5");return True
    if not 1<=count<=24:await u.message.reply_text("❌ Миналар 1-24.");return True
    x=u.effective_user;ensure(x)
    if x.id in mines_games:await u.message.reply_text("❌ Аяқталмаған ойын бар.");return True
    if not debit(x.id,n):await u.message.reply_text(f"❌ Қаражат жеткіліксіз: {money(balance(x.id))} ₸");return True
    g={"user_id":x.id,"bet":n,"mines":set(secrets.SystemRandom().sample(range(25),count)),"opened":set(),"safe":0};mines_games[x.id]=g
    await u.message.reply_text(f"💣 МИНАЛАР 5×5\n💰 {money(n)} ₸\n💣 Миналар: {count}\n📈 1.00x",reply_markup=board(g));return True

async def mine_click(u,c):
    q=u.callback_query;await q.answer();_,us,is_=q.data.split(":");uid=int(us);i=int(is_)
    if q.from_user.id!=uid or uid not in mines_games:await q.answer("❌ Бұл сенің ойының емес.",show_alert=True);return
    g=mines_games[uid]
    if i in g["opened"]:return
    if i in g["mines"]:
        del mines_games[uid];await q.edit_message_text(f"💥 МИНА!\n❌ {money(g['bet'])} ₸ жоғалттың.\n\n💣 МИНАЛАР:",reply_markup=board(g,True));return
    g["opened"].add(i);g["safe"]+=1;m=mine_mult(g["safe"],len(g["mines"]))
    if g["safe"]>=25-len(g["mines"]):
        p=int(g["bet"]*m);credit(uid,p);del mines_games[uid];await q.edit_message_text(f"🏆 БӘРІ АШЫЛДЫ!\n📈 {m:.2f}x\n💰 +{money(p)} ₸",reply_markup=board(g,True));return
    await q.edit_message_text(f"💣 МИНАЛАР 5×5\n💎 Ашылды: {g['safe']}\n📈 {m:.2f}x",reply_markup=board(g))

async def cash(u,c):
    q=u.callback_query;await q.answer();uid=int(q.data.split(":")[1])
    if q.from_user.id!=uid or uid not in mines_games:await q.answer("❌ Бұл сенің ойының емес.",show_alert=True);return
    g=mines_games[uid]
    if not g["safe"]:await q.answer("Алдымен ұяшық аш.",show_alert=True);return
    m=mine_mult(g["safe"],len(g["mines"]));p=int(g["bet"]*m);credit(uid,p);del mines_games[uid]
    await q.edit_message_text(f"💰 ҰТЫС АЛЫНДЫ!\n📈 {m:.2f}x\n+{money(p)} ₸\n\n💣 МИНАЛАР:",reply_markup=board(g,True))

async def createp(u,c):
    if u.effective_user.id!=OWNER_ID:await u.message.reply_text("❌ Тек бот иесіне.");return
    if len(c.args)!=3:await u.message.reply_text("/createp код саны сома");return
    try:code=c.args[0].lower();uses=int(c.args[1]);n=amount(c.args[2])
    except:await u.message.reply_text("❌ Қате параметрлер.");return
    with LOCK:
        try:db.execute("INSERT INTO promo_codes VALUES(?,?,0,?,?)",(code,uses,n,now()));db.commit()
        except sqlite3.IntegrityError:await u.message.reply_text("❌ Код бар.");return
    await u.message.reply_text(f"✅ /promo {code}\n👥 {uses}\n💰 {money(n)} ₸")

async def promo(u,c):
    x=u.effective_user;ensure(x)
    if len(c.args)!=1:await u.message.reply_text("/promo КОД");return
    code=c.args[0].lower()
    with LOCK:
        try:
            db.execute("BEGIN IMMEDIATE");p=db.execute("SELECT * FROM promo_codes WHERE code=?",(code,)).fetchone()
            if not p or p["used_count"]>=p["max_uses"]:db.rollback();await u.message.reply_text("❌ Промокод жарамсыз немесе лимит біткен.");return
            db.execute("INSERT INTO promo_uses VALUES(?,?)",(code,x.id));db.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?",(code,));db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(p["amount"],x.id));db.commit()
        except sqlite3.IntegrityError:db.rollback();await u.message.reply_text("❌ Сен бұл кодты бұрын қолдандың.");return
    await u.message.reply_text(f"🎁 +{money(p['amount'])} ₸")

async def text(u,c):
    t=u.message.text.strip();l=t.lower()
    if l in {"баланс","б","бал","ақша","қаражат"}:await bal(u,c);return
    if l in {"бонус","bonus","сыйлық","сый","дб","дейлик"}:await bonus(u,c);return
    if l in {"тарих","history","тарихы"}:await u.message.reply_text(hist());return
    if l.startswith(("мины ","миналар ")):
        await mines_start(u,t.split());return
    m=re.fullmatch(r"(\S+)\s+(.+)",t)
    if m:
        a,b=m.groups()
        if re.fullmatch(r"\d+(?:[.,]\d+)?(?:кк|kk|к|k|млн|м)?",a.lower()) and bet(b):
            await roulette(u,a,b);return
    # Неизвестные слова игнорируем.

async def transfer_text(u,c):
    pass

class Health(BaseHTTPRequestHandler):
    def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b"OK")
    def log_message(self,*a):pass

def health():
    ThreadingHTTPServer(("0.0.0.0",PORT),Health).serve_forever()

def main():
    if not TOKEN:raise RuntimeError("BOT_TOKEN environment variable is not set")
    threading.Thread(target=health,daemon=True).start()
    app=ApplicationBuilder().token(TOKEN).build()
    for cmd,fn in [("start",start),("help",helpcmd),("balance",bal),("bonus",bonus),("history",lambda u,c:u.message.reply_text(hist())),("promo",promo),("createp",createp)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(mine_click,pattern=r"^mine:"))
    app.add_handler(CallbackQueryHandler(cash,pattern=r"^cash:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))
    print("Бот іске қосылды.")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":main()
