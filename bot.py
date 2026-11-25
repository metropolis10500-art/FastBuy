#!/usr/bin/env python3
"""Друголуч: Telegram-бот. Python 3.10+, pip install requests python-dotenv."""
import os, time, json, sqlite3, logging, secrets, html, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN = int(os.getenv('ADMIN_ID', '0') or '0')
WALLET = os.getenv('YOOMONEY_WALLET', '').strip()
YM_TOKEN = os.getenv('YOOMONEY_TOKEN', '').strip()
SUPPORT = 'https://t.me/vladofix97'
PUBLIC_BASE = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
WEB_PORT = int(os.getenv('WEB_PORT', '8080'))
API = f'https://api.telegram.org/bot{TOKEN}/'
DB = sqlite3.connect(ROOT / 'bot.sqlite3')
DB.row_factory = sqlite3.Row
DB.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, joined INTEGER, inviter INTEGER, balance INTEGER DEFAULT 0, earned_ref INTEGER DEFAULT 0, earned_bonus INTEGER DEFAULT 0, bonus_at INTEGER DEFAULT 0, state TEXT DEFAULT '', temp TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS referrals(user_id INTEGER, ancestor INTEGER, level INTEGER, reward INTEGER, UNIQUE(user_id,ancestor));
CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER, amount INTEGER, method TEXT, details TEXT, status TEXT DEFAULT 'pending', created INTEGER);
CREATE TABLE IF NOT EXISTS ads(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,label TEXT UNIQUE,text TEXT DEFAULT '',status TEXT DEFAULT 'awaiting_payment', created INTEGER, expires INTEGER DEFAULT 0, operation_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS displayed_ads(chat_id INTEGER,message_id INTEGER,ad_id INTEGER,base TEXT,PRIMARY KEY(chat_id,message_id));
''')
DB.commit()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def now(): return int(time.time())
def esc(s): return html.escape(str(s or ''))
def money(v): return f'{v/100:,.2f}'.replace(',', ' ').replace('.', ',') + ' ₽'
def call(method, data=None, files=None):
    r=requests.post(API+method, data=data, files=files, timeout=40); r.raise_for_status(); j=r.json()
    if not j.get('ok'): raise RuntimeError(j.get('description', 'Telegram error'))
    return j.get('result')
def keyboard(rows): return json.dumps({'inline_keyboard':rows}, ensure_ascii=False)
def btn(text, data=None, url=None):
    return {'text':text, **({'callback_data':data} if data else {'url':url})}
def send(chat, text, rows=None):
    return call('sendMessage', {'chat_id':chat,'text':text,'parse_mode':'HTML','reply_markup':keyboard(rows or []), 'disable_web_page_preview':'true'})
def notify(text, rows=None):
    if ADMIN:
        try: send(ADMIN,text,rows)
        except Exception: logging.exception('Не удалось уведомить администратора')
def back(): return [[btn('◀️ Назад','menu')]]
def menu_rows(): return [[btn('🔗 Моя ссылка','link')],[btn('👤 Профиль','profile'),btn('🎁 Бонус','bonus')],[btn('💸 Вывод средств','withdraw')],[btn('📣 Заказать рекламу','advert')],[btn('❓ Как это работает','help')],[btn('🆘 Поддержка',url=SUPPORT)]]
def user(uid): return DB.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
def state(uid, value='', temp=''):
    DB.execute('UPDATE users SET state=?,temp=? WHERE id=?',(value,temp,uid)); DB.commit()
def register(u, ref=None):
    uid=u['id']; existing=user(uid)
    if existing:
        DB.execute('UPDATE users SET name=? WHERE id=?',(u.get('first_name','Друг'),uid)); DB.commit(); return
    inviter=int(ref) if ref and ref.isdigit() and int(ref)!=uid and user(int(ref)) else None
    with DB:
        DB.execute('INSERT INTO users(id,name,joined,inviter) VALUES(?,?,?,?)',(uid,u.get('first_name','Друг'),now(),inviter))
        parent=inviter
        for level,reward in enumerate((1500,800,400),1):
            if not parent: break
            DB.execute('INSERT INTO referrals VALUES(?,?,?,?)',(uid,parent,level,reward))
            DB.execute('UPDATE users SET balance=balance+?,earned_ref=earned_ref+? WHERE id=?',(reward,reward,parent))
            p=user(parent); parent=p['inviter'] if p else None

def show(chat, uid, page):
    u=user(uid); rows=back(); image=page; ad=None
    if page=='menu':
        text=f'👋 Привет, <b>{esc(u["name"])}</b>!\n\nПриглашай друзей по своей ссылке и получай вознаграждение:\n• <b>15 ₽</b> — за друга\n• <b>8 ₽</b> — за друга друга\n• <b>4 ₽</b> — за третий уровень\n\n💰 Баланс: <b>{money(u["balance"])}</b>'
        rows=menu_rows(); base=text
        ad=DB.execute("SELECT * FROM ads WHERE status='active' AND expires>? ORDER BY expires DESC LIMIT 1",(now(),)).fetchone()
        if ad: text+='\n\n📣 <b>Реклама</b>\n'+esc(ad['text'])
    elif page=='link':
        link=f'https://t.me/{BOT_USERNAME}?start=ref{uid}'
        count=DB.execute('SELECT COUNT(*) FROM referrals WHERE ancestor=? AND level=1',(uid,)).fetchone()[0]
        text=f'🔗 <b>Твоя реферальная ссылка</b>\n\n<code>{link}</code>\n\nЗа приглашённых: 15 ₽ / 8 ₽ / 4 ₽ по трём уровням.\n👥 Друзей первого уровня: {count}'
        rows=[[btn('📤 Поделиться',url='https://t.me/share/url?'+urlencode({'url':link}))]]+back()
    elif page=='profile':
        levels=DB.execute('SELECT level,COUNT(*) n,COALESCE(SUM(reward),0) s FROM referrals WHERE ancestor=? GROUP BY level',(uid,)).fetchall()
        data={r['level']:r for r in levels}
        pending=DB.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status='pending'",(uid,)).fetchone()[0]
        paid=DB.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status='paid'",(uid,)).fetchone()[0]
        text=f'👤 <b>Профиль</b>\n\n🆔 <code>{uid}</code>\n📅 В боте с {datetime.fromtimestamp(u["joined"],timezone.utc):%d.%m.%Y}\n\n💰 Баланс: <b>{money(u["balance"])}</b>\n⏳ На выводе: {money(pending)}\n✅ Выплачено: {money(paid)}\n\n📈 Заработано: {money(u["earned_ref"]+u["earned_bonus"])}\n• за друзей: {money(u["earned_ref"])}\n• бонусы: {money(u["earned_bonus"])}\n\n👥 Приглашения:'
        for level in (1,2,3):
            r=data.get(level); text+=f'\n{level} уровень: {r["n"] if r else 0} — {money(r["s"] if r else 0)}'
    elif page=='withdraw':
        text=f'💸 <b>Вывод средств</b>\n\n💰 Доступно: <b>{money(u["balance"])}</b>\nМинимальная сумма: <b>3 000 ₽</b>.\n\nЗаявки на карту или по СБП проверяются и выплачиваются вручную. Не отправляйте CVV, срок действия карты или коды подтверждения.'
        if u['balance']>=300000: rows=[[btn('💳 На карту','wd_card'),btn('📱 По СБП','wd_sbp')]]+back()
        else: text+=f'\n\n🔒 Не хватает {money(300000-u["balance"])}.'; rows=[[btn('🔗 Пригласить друзей','link')]]+back()
    elif page=='help':
        text='❓ <b>Как это работает</b>\n\n1️⃣ Скопируйте ссылку в разделе «Моя ссылка».\n2️⃣ Приглашайте друзей, которые ещё не запускали бота.\n3️⃣ Получайте 15 ₽, 8 ₽ и 4 ₽ за три уровня приглашений.\n4️⃣ Забирайте бонус 25 ₽ раз в 24 часа.\n\n💸 Вывод от 3 000 ₽ на карту или по СБП; заявки обрабатываются вручную. Накрутка и фейковые аккаунты запрещены.'
    elif page=='advert':
        image='menu'; text='📣 <b>Реклама в боте</b>\n\nСтоимость — <b>5 000 ₽</b>, срок — <b>72 часа</b>. После проверки оплаты и модерации текст появится в главном меню. Время отсчитывается с момента публикации.\n\nОплата поступает в кошелёк ЮMoney. Если сервис удерживает комиссию, на кошелёк должно поступить не менее 5 000 ₽. При ошибке оплаты обратитесь в поддержку.'
        rows=[[btn('📝 Создать заказ','ad_new')]]+back()
    else: return
    payload={'chat_id':chat,'caption':text,'parse_mode':'HTML','reply_markup':keyboard(rows)}
    with open(ROOT/'images'/f'{image}.jpg','rb') as f: result=call('sendPhoto',payload,{'photo':f})
    if ad:
        DB.execute('INSERT OR REPLACE INTO displayed_ads VALUES(?,?,?,?)',(chat,result['message_id'],ad['id'],base)); DB.commit()

def payment_link(label):
    return PUBLIC_BASE + '/pay?' + urlencode({'label':label})

class PayPage(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlsplit, parse_qs
        parts=urlsplit(self.path); label=parse_qs(parts.query).get('label',[''])[0]
        if parts.path != '/pay' or not label.startswith('ad') or len(label)!=26 or not WALLET.isdigit():
            self.send_error(404); return
        # The payment reference is random and only identifies the order; verification is server-side.
        form=(f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
              f'<title>Оплата рекламы</title><h1>Оплата рекламы: 5 000 ₽</h1>'
              f'<p>После оплаты вернитесь в бот и нажмите «Проверить оплату». '
              f'На кошелёк должно поступить не менее 5 000 ₽; возможна комиссия.</p>'
              f'<form method="POST" action="https://yoomoney.ru/quickpay/confirm">'
              f'<input type="hidden" name="receiver" value="{esc(WALLET)}">'
              f'<input type="hidden" name="quickpay-form" value="button">'
              f'<input type="hidden" name="sum" value="5000.00">'
              f'<input type="hidden" name="label" value="{esc(label)}">'
              f'<input type="hidden" name="successURL" value="https://t.me/{esc(BOT_USERNAME)}">'
              f'<button name="paymentType" value="PC">Кошелёк ЮMoney</button> '
              f'<button name="paymentType" value="AC">Банковская карта</button></form>')
        body=form.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, fmt, *args): pass

def verify(ad):
    r=requests.post('https://yoomoney.ru/api/operation-history',headers={'Authorization':'Bearer '+YM_TOKEN},data={'label':ad['label'],'records':100,'type':'deposition'},timeout=20); r.raise_for_status(); j=r.json()
    if j.get('error'): raise RuntimeError(j['error'])
    if 'operations' not in j: raise RuntimeError('ЮMoney не вернул список операций')
    for op in j['operations']:
        if op.get('label') != ad['label'] or op.get('status')!='success' or op.get('direction')!='in' or op.get('type') not in ('deposition','incoming-transfer'): continue
        try: amount=Decimal(str(op['amount']))
        except (KeyError,InvalidOperation): continue
        if amount >= Decimal('5000.00') and op.get('operation_id'): return op['operation_id']
    return None

def callbacks(q):
    uid=q['from']['id']; chat=q['message']['chat']['id']; d=q.get('data',''); register(q['from'])
    try: call('answerCallbackQuery',{'callback_query_id':q['id']})
    except Exception: pass
    if d in ('menu','link','profile','withdraw','help','advert'):
        state(uid); show(chat,uid,d)
    elif d=='bonus':
        with DB:
            changed=DB.execute('UPDATE users SET balance=balance+2500,earned_bonus=earned_bonus+2500,bonus_at=? WHERE id=? AND bonus_at<=?',(now(),uid,now()-86400)).rowcount
        if changed: send(chat,'🎁 Начислено <b>25 ₽</b>! Следующий бонус доступен через 24 часа.',back())
        else:
            remain=max(0,user(uid)['bonus_at']+86400-now()); send(chat,f'⏳ Следующий бонус через {remain//3600} ч {(remain%3600)//60} мин.',back())
    elif d in ('wd_card','wd_sbp'):
        if user(uid)['balance']<300000: send(chat,'Недостаточно средств.',back()); return
        state(uid,'wd_amount', 'карта' if d=='wd_card' else 'СБП'); send(chat,'Введите сумму вывода в рублях, например 3000.00. Для отмены нажмите «Назад».',back())
    elif d=='ad_new':
        if not WALLET or not YM_TOKEN or not PUBLIC_BASE.startswith('https://'): send(chat,'Приём оплаты ещё не настроен. Напишите в поддержку.',[[btn('🆘 Поддержка',url=SUPPORT)]]+back()); return
        state(uid,'ad_text'); send(chat,'📝 Пришлите текст рекламы одним сообщением (до 500 символов). Он будет отправлен на модерацию после оплаты.',back())
    elif d.startswith('ad_check:'):
        aid=int(d.split(':')[1]); ad=DB.execute('SELECT * FROM ads WHERE id=? AND user_id=?',(aid,uid)).fetchone()
        if not ad or ad['status']!='awaiting_payment': send(chat,'Заказ не найден или уже обработан.',back()); return
        try: op=verify(ad)
        except Exception:
            logging.exception('ЮMoney'); send(chat,'Не удалось проверить платёж. Попробуйте позже или обратитесь в поддержку.',back()); return
        if not op: send(chat,'Платёж пока не найден или на кошелёк поступило менее 5 000 ₽. Повторите проверку позже.',[[btn('🔄 Проверить оплату',f'ad_check:{aid}')]]+back()); return
        try:
            with DB: DB.execute("UPDATE ads SET status='moderation',operation_id=? WHERE id=? AND status='awaiting_payment'",(op,aid))
        except sqlite3.IntegrityError: send(chat,'Платёж уже использован.',back()); return
        send(chat,'✅ Оплата подтверждена. Реклама отправлена на модерацию.',back())
        notify(f'📣 Реклама #{aid}\nОт: <code>{uid}</code>\nОплата подтверждена ЮMoney.\n\n{esc(ad["text"])}',[[btn('✅ Опубликовать',f'approve_ad:{aid}'),btn('❌ Отклонить',f'reject_ad:{aid}')]])
    elif d.startswith(('approve_ad:','reject_ad:')):
        if uid!=ADMIN: return
        action, sid=d.split(':'); aid=int(sid); ad=DB.execute('SELECT * FROM ads WHERE id=?',(aid,)).fetchone()
        if not ad or ad['status']!='moderation': send(chat,'Заказ уже обработан.'); return
        if action=='approve_ad':
            active=DB.execute("SELECT id FROM ads WHERE status='active' AND expires>?",(now(),)).fetchone()
            if active: send(chat,'Сейчас уже размещена реклама. Повторите после её окончания.'); return
            with DB: DB.execute("UPDATE ads SET status='active',expires=? WHERE id=?",(now()+72*3600,aid))
            send(chat,f'✅ Реклама #{aid} размещена на 72 часа.'); send(ad['user_id'],'✅ Реклама опубликована в главном меню на 72 часа.')
        else:
            with DB: DB.execute("UPDATE ads SET status='rejected' WHERE id=?",(aid,))
            send(chat,'Реклама отклонена. Возврат оплаты проводится вручную.'); send(ad['user_id'],'❌ Реклама отклонена. Для возврата оплаты обратитесь в поддержку.',[[btn('🆘 Поддержка',url=SUPPORT)]])
    elif d.startswith(('paid:','reject_wd:')):
        if uid!=ADMIN: return
        action,sid=d.split(':'); wid=int(sid)
        with DB:
            w=DB.execute('SELECT * FROM withdrawals WHERE id=?',(wid,)).fetchone()
            if not w or w['status']!='pending': send(chat,'Заявка уже обработана.'); return
            DB.execute('UPDATE withdrawals SET status=? WHERE id=?',('paid' if action=='paid' else 'rejected',wid))
            if action=='reject_wd': DB.execute('UPDATE users SET balance=balance+? WHERE id=?',(w['amount'],w['user_id']))
        send(chat,'Статус заявки обновлён.'); send(w['user_id'],f'Заявка на вывод #{wid}: '+('✅ выплачена.' if action=='paid' else '❌ отклонена; сумма возвращена на баланс.'))

def messages(m):
    if 'from' not in m or m.get('chat',{}).get('type')!='private': return
    uid=m['from']['id']; chat=m['chat']['id']; text=(m.get('text') or '').strip()
    if text.startswith('/start'):
        parts=text.split(maxsplit=1); ref=parts[1][3:] if len(parts)>1 and parts[1].startswith('ref') else None
        register(m['from'],ref); state(uid); show(chat,uid,'menu'); return
    register(m['from']); u=user(uid)
    if text=='/admin' and uid==ADMIN:
        ws=DB.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 30").fetchall()
        ads=DB.execute("SELECT * FROM ads WHERE status='moderation' ORDER BY id LIMIT 30").fetchall()
        send(chat,f'🛠 Ожидают: выплат — {len(ws)}, реклам — {len(ads)}.')
        for w in ws: notify_wd(w)
        for ad in ads: notify(f'📣 Реклама #{ad["id"]}\n{esc(ad["text"])}',[[btn('✅ Опубликовать',f'approve_ad:{ad["id"]}'),btn('❌ Отклонить',f'reject_ad:{ad["id"]}')]])
        return
    if u['state']=='ad_text':
        if not text or len(text)>500: send(chat,'Нужен текст до 500 символов.'); return
        label='ad'+secrets.token_hex(12)
        with DB: cur=DB.execute('INSERT INTO ads(user_id,label,text,created) VALUES(?,?,?,?)',(uid,label,text,now())); aid=cur.lastrowid
        state(uid); send(chat,f'📣 Заказ #{aid}\n\n{esc(text)}\n\nОплатите 5 000 ₽ и затем проверьте платёж. Зачисленная сумма должна быть не менее 5 000 ₽.',[[btn('💳 Оплатить через ЮMoney',url=payment_link(label))],[btn('🔄 Проверить оплату',f'ad_check:{aid}')]]+back()); return
    if u['state']=='wd_amount':
        try:
            amount=Decimal(text.replace(',','.')); kop=amount*100
            if not amount.is_finite() or kop!=kop.to_integral_value() or kop<300000 or kop>u['balance']: raise ValueError()
            value=int(kop)
        except (InvalidOperation,ValueError,OverflowError): send(chat,f'Введите сумму от 3 000 ₽ до {money(u["balance"])} с точностью до копеек.'); return
        state(uid,'wd_details',json.dumps({'amount':value,'method':u['temp']},ensure_ascii=False))
        send(chat,'Укажите номер карты и банк или номер телефона и банк для СБП. Не отправляйте CVV, срок действия и коды подтверждения.'); return
    if u['state']=='wd_details':
        if not text or len(text)>150: send(chat,'Укажите реквизиты текстом, не более 150 символов.'); return
        info=json.loads(u['temp']); amount=info['amount']
        with DB:
            updated=DB.execute('UPDATE users SET balance=balance-?,state="",temp="" WHERE id=? AND balance>=?',(amount,uid,amount)).rowcount
            if not updated: send(chat,'Баланс изменился, создайте заявку заново.',back()); return
            cur=DB.execute('INSERT INTO withdrawals(user_id,amount,method,details,created) VALUES(?,?,?,?,?)',(uid,amount,info['method'],text,now())); wid=cur.lastrowid
        send(chat,f'✅ Заявка #{wid} создана. Выплата выполняется вручную.',back())
        notify_wd(DB.execute('SELECT * FROM withdrawals WHERE id=?',(wid,)).fetchone()); return
    send(chat,'Используйте /start, чтобы открыть меню.')

def notify_wd(w):
    notify(f'💸 Заявка #{w["id"]}\nПользователь: <code>{w["user_id"]}</code>\nСумма: {money(w["amount"])}\nСпособ: {esc(w["method"])}\nРеквизиты: <code>{esc(w["details"])}</code>\n\nСначала переведите деньги вручную, затем отметьте выплату.',[[btn('✅ Выплачено',f'paid:{w["id"]}'),btn('❌ Отклонить',f'reject_wd:{w["id"]}')]])

def expire_ads():
    with DB: DB.execute("UPDATE ads SET status='expired' WHERE status='active' AND expires<=?",(now(),))
    rows=DB.execute("SELECT d.* FROM displayed_ads d JOIN ads a ON a.id=d.ad_id WHERE a.status!='active' OR a.expires<=? LIMIT 100",(now(),)).fetchall()
    for r in rows:
        try:
            call('editMessageCaption',{'chat_id':r['chat_id'],'message_id':r['message_id'],'caption':r['base'],'parse_mode':'HTML','reply_markup':keyboard(menu_rows())})
        except Exception as e:
            # Deleted/blocked messages cannot display the ad; transient errors are retried.
            s=str(e).lower()
            if not any(x in s for x in ('message to edit not found','message is not modified','message can\'t be edited','bot was blocked','chat not found')): logging.warning('Повторим удаление рекламы: %s',e); continue
        with DB: DB.execute('DELETE FROM displayed_ads WHERE chat_id=? AND message_id=?',(r['chat_id'],r['message_id']))

def main():
    global BOT_USERNAME
    if not TOKEN or not ADMIN: raise SystemExit('Заполните BOT_TOKEN и числовой ADMIN_ID в .env')
    BOT_USERNAME=call('getMe')['username']; logging.info('Запущен @%s',BOT_USERNAME)
    if PUBLIC_BASE:
        server=ThreadingHTTPServer(('0.0.0.0',WEB_PORT),PayPage)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        logging.info('Платёжная страница: порт %s',WEB_PORT)
    call('deleteWebhook',{'drop_pending_updates':'false'})
    offset=0; last=0
    while True:
        try:
            if now()-last>=30: expire_ads(); last=now()
            updates=call('getUpdates',{'offset':offset,'timeout':20,'allowed_updates':json.dumps(['message','callback_query'])})
            for update in updates:
                offset=update['update_id']+1
                try:
                    if 'callback_query' in update: callbacks(update['callback_query'])
                    elif 'message' in update: messages(update['message'])
                except Exception: logging.exception('Ошибка обработки update %s',update['update_id'])
        except Exception: logging.exception('Ошибка опроса'); time.sleep(5)
if __name__=='__main__': main()
