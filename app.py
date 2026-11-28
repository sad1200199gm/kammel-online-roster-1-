from storage import Store
import os, re, secrets, threading, time, unicodedata
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, jsonify, redirect, render_template_string, request, send_from_directory

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32768
ROOT = Path(__file__).resolve().parent
PASSWORD = os.environ.get('GAME_ADMIN_KEY', '')
ADMIN_USER = os.environ.get('GAME_ADMIN_USER', 'admin').strip() or 'admin'
def origin_url(value):
    try:
        u = urlsplit(value.strip().rstrip('/'))
        port = u.port
        if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.path or u.query or u.fragment:
            return ''
        host = u.hostname.lower()
        if not re.fullmatch(r'[a-z0-9.-]+', host):
            return ''
        suffix = f':{port}' if port and port != {'http':80,'https':443}[u.scheme] else ''
        return f'{u.scheme}://{host}{suffix}'
    except ValueError:
        return ''

RENDER_URL = origin_url(os.getenv('RENDER_EXTERNAL_URL', ''))
CUSTOM_URL = origin_url(os.getenv('GAME_PUBLIC_URL', ''))
TRUSTED = {u for u in (RENDER_URL, CUSTOM_URL) if u and (not os.getenv('RENDER') or u.startswith('https://'))}
PUBLIC = next((u for u in (CUSTOM_URL, RENDER_URL) if u in TRUSTED), '')
COOKIE = 'kammel_host'
SESSION = secrets.token_urlsafe(32)
CODES = {}
ROOM = {}
PLAYERS, LOGIN_ATTEMPTS, JOIN_ATTEMPTS = {}, defaultdict(deque), defaultdict(deque)
LOCK = threading.Lock()
STATE = {'active': False, 'id': '', 'status': 'بانتظار القائد'}
MATCH, REVISION = None, 0
STATS = {}
DEADLINE, SOLUTION = 0, ''
STORE = Store()
saved = STORE.load()
ROOM, CODES, PLAYERS = (saved.get(k, {}) for k in ('room','codes','players'))
STATE = saved.get('state',STATE)
CARD_EVENTS = deque(saved.get('cardEvents',[]), maxlen=40)

def recent_cards(player_id=None):
    now = time.time()
    return [dict(e) for e in CARD_EVENTS if now-e['at']<=20 and (player_id is None or e['playerId']==player_id)]
MATCH, REVISION = saved.get('match'), saved.get('revision',0)
STATS = saved.get('stats',{}) or {}
DEADLINE, SOLUTION = saved.get('deadline',0), saved.get('solution','')

def persist():
    STORE.save(dict(room=ROOM,codes=CODES,players={k:{f:p[f] for f in ('id','name','team','status')} | {'sent':0,'cards':p.get('cards',0)} for k,p in PLAYERS.items()},cardEvents=list(CARD_EVENTS),state=STATE,match=MATCH,revision=REVISION,deadline=DEADLINE,solution=SOLUTION,stats=STATS))

def finish_round(why, team=None, winner=None):
    global REVISION, SOLUTION
    if not STATE.get('active') or not MATCH:
        return
    award = int(MATCH['settings']['reward']) if team else 0
    points = MATCH['points']
    if team:
        points[team] += award
    goal = int(MATCH['settings']['target'])
    win = max(points,key=points.get) if max(points.values())>=goal and points['a']!=points['b'] else None
    retry = MATCH['stage']==4
    result = why + (f' (+{award})' if team else '') + ' — الحل: ' + MATCH['answer']
    status = result + ' • ' + (('🏆 فاز فريق '+('أ' if win=='a' else 'ب')) if win else ('بانتظار القائد لبدء جولة إضافية' if retry else 'بانتظار القائد لبدء الجولة التالية'))
    log = MATCH.get('log') or []
    log.append('الجولة '+str((MATCH['stage'] if isinstance(MATCH.get('stage'),int) else 0)+1)+' — '+result)
    MATCH['log'] = log[-60:]
    MATCH.update(running=False, mask=[], win=win, allDone=bool(win), finalRetry=retry,lastResult=result,status=status)
    MATCH['frozen'] = {}
    MATCH['freezeUsed'] = {}
    STATE.update(active=False,image='',mask='',status=status,score=f"فريق أ: {points['a']} — فريق ب: {points['b']}")
    STATE.pop('paused',None)
    STATE.pop('pausedAt',None)
    STATE.pop('roundWinner',None)
    if winner and team:
        STATE['roundWinner'] = {'name':winner['name'],'team':team}
    STATE['roundReaction'] = {'id':STATE['id'],'team':team,'expires':time.time()+6,'winner':{'id':winner['id'],'name':winner['name']} if winner else None}
    SOLUTION=''
    REVISION+=1
    persist()

def expire():
    if STATE.get('active') and not STATE.get('paused') and time.time()>=DEADLINE:
        finish_round('انتهى الوقت، بلا نقاط')

LOGIN = '''<!doctype html><html dir="rtl" lang="ar"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>لوحة قائد كمّل الكلمة</title><style>body{background:#131b2f;color:#fff;font:18px system-ui;margin:0;padding:1rem}main{max-width:28rem;margin:10vh auto;background:#222e50;padding:1.5rem;border-radius:1rem}input,button{box-sizing:border-box;width:100%;padding:.7rem;margin:.4rem 0;font:inherit}button{background:#e6fa88;border:0;font-weight:bold}</style><main><h1>كمّل الكلمة</h1><p>دخول الإدارة</p>{% if note %}<p role="alert">{{ note }}</p>{% endif %}<form method="post" action="/admin"><label>اسم المستخدم<input name="username" required autocomplete="username" maxlength="100"></label><label>كلمة المرور<input type="password" name="key" required autocomplete="current-password" maxlength="256"></label><button>دخول الإدارة</button></form><p><a style="color:#c8e1ff" href="/">الرجوع إلى تسجيل اللاعبين</a></p></main></html>'''

SETUP = '''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>إنشاء غرفة — كمّل الكلمة</title><style>*{box-sizing:border-box}body{background:#131b2f;color:#fff;font:18px system-ui;margin:0;padding:1rem}main{max-width:29rem;margin:7vh auto;background:#222e50;padding:1.5rem;border:1px solid #8292bd;border-radius:1rem}input,button{display:block;width:100%;padding:.7rem;margin:.4rem 0 1rem;font:inherit;border-radius:.6rem}button{background:#e6fa88;border:0;font-weight:bold;cursor:pointer}</style><main><h1>أنشئ غرفة اللعبة</h1><p>اكتب اسمك واسم الغرفة. بعدها يظهر باركود فريق أ وباركود فريق ب.</p>{% if note %}<p role="alert">{{ note }}</p>{% endif %}<form method="post" action="/setup"><label>اسم القائد<input name="leader" maxlength="30" required autocomplete="nickname" value="{{ leader }}"></label><label>اسم الغرفة<input name="room" maxlength="40" required value="{{ room }}"></label><button>إنشاء الغرفة وإظهار الباركود</button></form></main></html>'''

def norm(s):
    return re.sub(r'[\u064b-\u065f\u0670\u0640\s]', '', unicodedata.normalize('NFKC', s)).translate(str.maketrans('أإآٱىة', 'اااايه'))

def err(msg, status):
    return jsonify(error=msg), status

def admin():
    cookie = request.cookies.get(COOKIE, '')
    return bool(PASSWORD and cookie and secrets.compare_digest(cookie.encode(), SESSION.encode()))

def token():
    bearer = request.headers.get('Authorization', '')
    return bearer[7:] if bearer.startswith('Bearer ') else ''

def permitted_source():
    origin = request.headers.get('Origin')
    if not origin:
        return True
    allowed = TRUSTED or {origin_url(request.host_url)}
    candidate = origin_url(origin)
    return bool(candidate and candidate in allowed)

def attempts(buckets, cap, window):
    now = time.monotonic()
    if os.getenv('RENDER'):
        ip = request.headers.get('X-Forwarded-For', '').split(',')[-1].strip() or request.remote_addr
    else:
        ip = request.remote_addr
    if len(buckets) >= 2048 and ip not in buckets:
        for k, q in list(buckets.items()):
            if not q or q[-1] <= now-window:
                del buckets[k]
        if len(buckets) >= 2048:
            return False
    q = buckets[ip]
    while q and q[0] <= now-window:
        q.popleft()
    if len(q) >= cap:
        return False
    q.append(now)
    return True

@app.after_request
def headers(r):
    r.headers['Cache-Control'] = 'no-store'
    # Preserve the origin for same-site HTML form submissions without sending a referrer off-site.
    r.headers['Referrer-Policy'] = 'same-origin'
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['X-Frame-Options'] = 'DENY'
    return r

@app.get('/health')
def health():
    return 'ok' if PASSWORD else ('missing GAME_ADMIN_KEY', 503)

@app.get('/')
def register_page():
    return send_from_directory(ROOT, 'player.html')

@app.route('/admin', methods=['GET','POST'])
def login():
    if admin():
        return redirect('/host', 303)
    if not PASSWORD:
        return render_template_string(LOGIN, note='اضبط مفتاح القائد في إعدادات الاستضافة أولًا.'), 503
    if request.method == 'POST':
        if not permitted_source():
            return render_template_string(LOGIN, note='عنوان الصفحة لا يطابق عنوان اللعبة المسموح. افتح رابط الخدمة مباشرة من لوحة Render ثم حاول مجددًا.'), 403
        with LOCK:
            if not attempts(LOGIN_ATTEMPTS, 15, 120):
                return render_template_string(LOGIN, note='محاولات كثيرة. انتظر دقيقتين.'), 429
        value = request.form.get('key', '')
        user = request.form.get('username', '').strip()
        if len(user)<=100 and len(value)<=256 and secrets.compare_digest(user.encode(), ADMIN_USER.encode()) and secrets.compare_digest(value.encode(), PASSWORD.encode()):
            r = redirect('/host', 303)
            r.set_cookie(COOKIE, SESSION, httponly=True, secure=PUBLIC.startswith('https://') or request.is_secure, samesite='Strict')
            return r
        return render_template_string(LOGIN, note='اسم المستخدم أو كلمة المرور غير صحيحة.'), 403
    return render_template_string(LOGIN, note='')

@app.route('/setup', methods=['GET','POST'])
def setup():
    if not admin():
        return redirect('/admin',303)
    if request.method == 'POST' and not permitted_source():
        return err('الطلب غير صالح',403)
    with LOCK:
        if ROOM:
            return redirect('/host',303)
        if request.method == 'GET':
            return render_template_string(SETUP, note='', leader='', room='')
        leader, room = (request.form.get(k,'').strip() for k in ('leader','room'))
        if not (1<=len(leader)<=30 and 1<=len(room)<=40):
            return render_template_string(SETUP, note='اكتب اسم القائد واسم الغرفة ضمن الحد المسموح.', leader=leader[:30], room=room[:40]), 400
        ROOM.update(leader=leader, name=room)
        CODES.update({t:secrets.token_urlsafe(14) for t in ('a','b')})
        persist()
    return redirect('/host',303)

@app.get('/host')
def host():
    if not admin():
        return redirect('/admin', 303)
    if not ROOM:
        return redirect('/setup',303)
    return send_from_directory(ROOT, 'host.html')

@app.get('/join/<team>/<code>')
def player(team, code):
    if CODES.get(team) != code:
        return 'رابط الفريق غير صالح أو انتهت صلاحيته. امسح الباركود الجديد من القائد.', 404, {'Content-Type':'text/plain; charset=utf-8'}
    return send_from_directory(ROOT, 'player.html')

@app.get('/api/config')
def config():
    if not admin():
        return err('خاص بالقائد', 403)
    origin = PUBLIC or request.host_url.rstrip('/')
    with LOCK:
        if not ROOM:
            return err('أنشئ الغرفة أولًا',409)
        return jsonify(room=ROOM, urls={t:f'{origin}/join/{t}/{c}' for t,c in CODES.items()})

@app.get('/api/invite/<team>/<code>')
def invite(team, code):
    with LOCK:
        if not ROOM or CODES.get(team) != code:
            return err('رابط الفريق غير صالح',404)
        return jsonify(room=ROOM,team=team)

@app.get('/roster')
@app.get('/roster/<team>/<code>')
def roster_page(team=None, code=None):
    if team is None:
        if admin() and not ROOM:
            return redirect('/setup', 303)
    elif not ROOM or CODES.get(team) != code:
        return err('رابط الفريق غير صالح', 404)
    return send_from_directory(ROOT, 'roster.html')

@app.get('/api/roster')
def roster_data():
    with LOCK:
        p = PLAYERS.get(token())
        if not ROOM or (not admin() and (not p or p['status'] != 'accepted')):
            return err('قائمة اللاعبين متاحة بعد قبول طلب الانضمام.', 403)
        now = time.monotonic()
        if p and p['status'] == 'accepted':
            p['seen'] = now
        members = [dict(name=v['name'], team=v['team'], online=v.get('seen') is not None and now-v['seen'] <= 15) for v in PLAYERS.values() if v['status'] == 'accepted']
        return jsonify(room=ROOM, players=members, cardEvents=recent_cards())

def online_player_count():
    now = time.monotonic()
    return sum(p['status']=='accepted' and p.get('seen') is not None and now-p['seen']<=15 for p in PLAYERS.values())

def stats_list(limit=40):
    rows = sorted(STATS.items(), key=lambda kv:(-kv[1].get('correct',0), kv[1].get('fastest') if kv[1].get('fastest') is not None else 1<<30))
    return [dict(id=k, **v) for k,v in rows[:limit]]

def checkpoint():
    if STATE.get('active'):
        remaining = max(0,round((DEADLINE-STATE.get('pausedAt',time.time()))*1000)) if STATE.get('paused') else max(0,round((DEADLINE-time.time())*1000))
    else:
        remaining = 0
    return dict(revision=REVISION, snapshot=MATCH, onlineCount=online_player_count(), remainingMs=remaining, paused=bool(STATE.get('paused')), stats=stats_list())

@app.get('/api/match')
def match_state():
    if not admin():
        return err('خاص بالقائد',403)
    with LOCK:
        expire()
        return jsonify(checkpoint())

@app.get('/api/player')
def status():
    with LOCK:
        expire()
        p = PLAYERS.get(token())
        if not p:
            return err('أعد فتح رابط الدعوة وسجّل من جديد',403)
        now = time.monotonic()
        if p['status'] == 'accepted':
            p['seen'] = now
        game = dict(STATE) if p['status'] == 'accepted' else None
        reaction = game.pop('roundReaction',None) if game else None
        if reaction and time.time()<reaction['expires']:
            game['reaction'] = {'id':reaction['id'],'kind':'clap' if p['team']==reaction['team'] else 'aww'}
            winner = reaction.get('winner') or {}
            if winner.get('id') == p['id']:
                game['correctCelebration'] = {'id':reaction['id'],'name':winner['name'],'team':p['team']}
        if game and game.get('active'):
            if STATE.get('paused'):
                game['paused'] = True
                game['remainingMs'] = max(0, round((DEADLINE-STATE.get('pausedAt',time.time()))*1000))
                game['left'] = 'متوقفة مؤقتًا'
            else:
                left = max(0, int(DEADLINE-time.time()+.9999))
                game['remainingMs'] = max(0, round((DEADLINE-time.time())*1000))
                game['left'] = str(left)+' ث'
            hint = (MATCH or {}).get('teamHints',{}).get(p['team'])
            if hint:
                game['teamHint'] = hint
            game['hintRequest'] = (MATCH or {}).get('hintRequests',{}).get(p['team'],{}).get('status','')
            game['hintAvailable'] = hint_available(MATCH)
            game['canRequestHint'] = game['hintAvailable'] and (MATCH or {}).get('points',{}).get(p['team'],0) >= 50
            game['frozen'] = bool((MATCH or {}).get('frozen',{}).get(p['team']))
            game['freezeUsed'] = bool((MATCH or {}).get('freezeUsed',{}).get(p['team']))
            game['canFreeze'] = not game['freezeUsed'] and (MATCH or {}).get('points',{}).get(p['team'],0) >= 150
            if not STATE.get('paused') and time.time() >= DEADLINE:
                game['active'] = False
        if game and not game.get('active'):
            game['image'] = ''
            game['mask'] = ''
        cards = recent_cards() if p['status']=='accepted' else recent_cards(p['id']) if p['status']=='kicked' else []
        return jsonify(status=p['status'],name=p['name'],team=p['team'],game=game,room=ROOM,cardEvents=cards,cards=p.get('cards',0))

def hint_available(match):
    if not match:
        return False
    mask = match.get('mask', [])
    if '؟' in mask:
        return mask.count('؟') >= 2
    if match.get('stage') == 4:
        return bool(match.get('imageHint')) if not mask else sum(not c.isspace() for c in match.get('answer','')) - len(mask) >= 2
    return False

@app.post('/api/<action>')
def action(action):
    global STATE, SOLUTION, DEADLINE, MATCH, REVISION
    if not permitted_source():
        return err('الطلب غير صالح',403)
    j = request.get_json(silent=True)
    if not isinstance(j,dict):
        return err('الطلب غير صالح',400)
    if action not in ('join','answer','request-hint','freeze') and not admin():
        return err('خاص بالقائد',403)
    with LOCK:
        expire()
        if action == 'join':
            t, code, name = j.get('team'), j.get('code'), j.get('name')
            if not ROOM:
                return err('ما فيه غرفة مفتوحة الآن. جرّب بعد ما يفتح المدير الغرفة.',409)
            direct = code is None
            if not isinstance(t,str) or t not in CODES or (not direct and (not isinstance(code,str) or not secrets.compare_digest(code.encode(),CODES[t].encode()))):
                return err('رمز الفريق غير صالح',403)
            if not isinstance(name,str) or not 1 <= len(name.strip()) <= 25:
                return err('اكتب اسمًا من ١ إلى ٢٥ حرفًا',400)
            if not attempts(JOIN_ATTEMPTS, 12, 60):
                return err('انتظر لحظة قبل التسجيل من جديد',429)
            if len(PLAYERS)>=160:
                dead = next((k for k,v in PLAYERS.items() if v['status'] in ('rejected','kicked')),None)
                if not dead:
                    return err('الغرفة ممتلئة',409)
                del PLAYERS[dead]
            key = secrets.token_urlsafe(24)
            PLAYERS[key] = dict(id=secrets.token_hex(8),name=name.strip(),team=t,status='pending',sent=0)
            persist()
            return jsonify(token=key, room=ROOM)
        if action == 'answer':
            p = PLAYERS.get(token())
            if not p or p['status']!='accepted':
                return err('المشاركة تتطلب موافقة القائد',403)
            now = time.monotonic()
            if not STATE.get('active') or j.get('id')!=STATE.get('id') or time.time()>=DEADLINE:
                return err('الجولة مغلقة',409)
            if STATE.get('paused'):
                return err('الجولة متوقفة مؤقتًا من القائد',409)
            if (MATCH or {}).get('frozen',{}).get(p['team']):
                return err('تم إيقاف فريقكم هذا السؤال. خلك بسعد، الجولة الجاية أنتم الأقوى!',409)
            ans = j.get('answer')
            if not isinstance(ans,str) or not 1<=len(ans.strip())<=45:
                return err('اكتب إجابة صالحة',400)
            if p.get('sent_round') == STATE['id'] and now-p['sent']<.6 and p.get('attempts',0)<5:
                return err('انتظر لحظة ثم حاول مرة ثانية',429)
            p['sent']=now
            p['sent_round']=STATE['id']
            if p.get('attempts_round') != STATE['id']:
                p['attempts_round'] = STATE['id']
                p['attempts'] = 0
            if p['attempts'] >= 5:
                return err('خلصت محاولاتك الخمس لهذا اللغز. تمهّل وفكّر زين، الجولة الجاية فرصتك.',409)
            if p.get('attempts_round') != STATE['id']:
                p['attempts_round'] = STATE['id']
                p['attempts'] = 0
            if p['attempts'] >= 5:
                return err('خلصت محاولاتك الخمس لهذا اللغز. تمهّل وفكّر زين، الجولة الجاية فرصتك.',409)
            alternatives = (MATCH or {}).get('aliases', [])
            accepted = bool(SOLUTION and (norm(ans)==SOLUTION or any(norm(ans).casefold()==norm(v).casefold() for v in alternatives)))
            if accepted:
                st = STATS.setdefault(p['id'],{'name':p['name'],'team':p['team'],'correct':0,'fastest':None})
                st['name'], st['team'] = p['name'], p['team']
                st['correct'] += 1
                elapsed = int((time.time()-STATE.get('startedAt',time.time()))*1000)
                if st['fastest'] is None or elapsed < st['fastest']:
                    st['fastest'] = elapsed
                finish_round('⚡ أسرع إجابة صحيحة: '+p['name']+' — فريق '+('أ' if p['team']=='a' else 'ب'), p['team'], p)
            else:
                p['attempts'] = p.get('attempts',0)+1
            return jsonify(ok=True,correct=accepted,attemptsLeft=max(0,5-p.get('attempts',0)))
        if action == 'yellow-card':
            p = next((x for x in PLAYERS.values() if x['id']==j.get('id') and x['status']=='accepted'),None)
            if not p:
                return err('اللاعب غير موجود ضمن المقبولين',409)
            count = p.get('cards',0)
            if type(j.get('expected')) is not int or j['expected']!=count or count>=3:
                return err('عدد الكروت تغيّر. انتظر تحديث القائمة.',409)
            count += 1
            p['cards'] = count
            if count == 3:
                p['status'] = 'kicked'
            event = dict(id=secrets.token_hex(10),playerId=p['id'],name=p['name'],team=p['team'],count=count,at=time.time())
            CARD_EVENTS.append(event)
            persist()
            return jsonify(event=event)
        if action == 'decide':
            for p in PLAYERS.values():
                if p['id']==j.get('id') and ((p['status']=='pending' and j.get('action') in ('accepted','rejected')) or (p['status']=='accepted' and j.get('action')=='kicked')):
                    p['status']=j['action']
                    persist()
                    return jsonify(ok=True)
            return err('الطلب تغيّر بالفعل',409)
        if action == 'request-hint':
            p = PLAYERS.get(token())
            if not p or p['status'] != 'accepted':
                return err('طلب التلميح متاح للاعبين المقبولين فقط',403)
            if not STATE.get('active') or not MATCH or j.get('id') != STATE.get('id'):
                return err('السؤال انتهى أو تغيّر',409)
            team = p['team']
            if not hint_available(MATCH):
                return err('التلميح الإضافي غير متاح لأنه سيكشف الحل كاملًا.',409)
            requests = MATCH.setdefault('hintRequests',{})
            if team in requests or team in MATCH.get('teamHints',{}):
                return err('فريقك أرسل طلب تلميح لهذا السؤال بالفعل',409)
            if MATCH['points'][team] < 50:
                return err('رصيد فريقك أقل من ٥٠ نقطة',409)
            requests[team] = {'status':'pending','name':p['name']}
            REVISION += 1
            persist()
            return jsonify(ok=True)
        if action == 'freeze':
            p = PLAYERS.get(token())
            if not p or p['status'] != 'accepted':
                return err('إيقاف الجولة متاح للاعبين المقبولين فقط',403)
            if not STATE.get('active') or not MATCH or j.get('id') != STATE.get('id'):
                return err('الإيقاف متاح خلال السؤال الحالي فقط',409)
            team = p['team']
            other = 'b' if team=='a' else 'a'
            if MATCH.get('freezeUsed',{}).get(team):
                return err('فريقكم استخدم إيقاف الجولة هذا السؤال بالفعل',409)
            if MATCH['points'][team] < 150:
                return err('رصيد فريقكم أقل من ١٥٠ نقطة',409)
            MATCH.setdefault('freezeUsed',{})[team] = True
            MATCH.setdefault('frozen',{})[other] = True
            MATCH['freezeNote'] = f"🚫 فريق {'أ' if team=='a' else 'ب'} جمّد جولة فريق {'أ' if other=='a' else 'ب'} — خُصمت ١٥٠ نقطة من فريق {'أ' if team=='a' else 'ب'}."
            log = MATCH.get('log') or []
            log.append(MATCH['freezeNote'])
            MATCH['log'] = log[-60:]
            MATCH['points'][team] -= 150
            STATE['score'] = f"فريق أ: {MATCH['points']['a']} — فريق ب: {MATCH['points']['b']}"
            REVISION += 1
            persist()
            return jsonify(ok=True)
        if action == 'team-hint':
            team = j.get('team')
            if team not in ('a','b'):
                return err('اختر فريق أ أو فريق ب',400)
            if not STATE.get('active') or not MATCH or j.get('id') != STATE.get('id'):
                return err('التلميح متاح خلال السؤال الحالي فقط',409)
            decision = j.get('decision')
            if decision not in ('approve','reject'):
                return err('اختر الموافقة أو الرفض',400)
            req = MATCH.get('hintRequests',{}).get(team,{})
            if req.get('status') != 'pending':
                return err('ما فيه طلب تلميح معلّق لهذا الفريق',409)
            if decision == 'reject':
                req['status'] = 'rejected'
                REVISION += 1
                persist()
                return jsonify(checkpoint())
            if not any(p['status']=='accepted' and p['team']==team for p in PLAYERS.values()):
                return err('ما فيه لاعب مقبول في هذا الفريق',409)
            hints = MATCH.get('teamHints',{})
            if team in hints:
                return err('هذا الفريق أخذ تلميح هذا السؤال بالفعل',409)
            points = MATCH['points']
            if points[team] < 50:
                return err('رصيد الفريق أقل من ٥٠ نقطة',409)
            answer = MATCH['answer']
            mask = MATCH.get('mask',[])
            letters = [(idx,c) for idx,c in enumerate(answer) if not c.isspace()]
            if MATCH['stage']==4 and not mask:
                clue = MATCH.get('imageHint')
                if not isinstance(clue,str) or not clue.strip():
                    return err('ما فيه تلميح وصفي متاح لهذا السؤال. لم تُخصم أي نقاط.',409)
                hint = clue.strip()[:240]
            else:
                if MATCH['stage']==4 and '؟' not in mask:
                    missing = list(enumerate(letters[len(mask):],start=len(mask)+1))
                else:
                    missing = [(i,(idx,c)) for i,(idx,c) in enumerate(letters,1) if idx<len(mask) and mask[idx]=='؟']
                if len(missing)<2:
                    return err('ما فيه حرف إضافي يمكن كشفه بدون إظهار الحل كاملًا',409)
                pos, (_, letter) = missing[0]
                hint = f'الحرف رقم {pos} هو «{letter}» (بدون حساب المسافات).'
            MATCH['teamHints'] = dict(hints, **{team:hint})
            req['status'] = 'approved'
            points[team] -= 50
            STATE['score'] = f"فريق أ: {points['a']} — فريق ب: {points['b']}"
            REVISION += 1
            persist()
            return jsonify(checkpoint())
        if action == 'pause-round':
            if not STATE.get('active') or not STATE.get('id'):
                return err('ما فيه جولة جارية لإيقافها',409)
            if j.get('pause'):
                if not STATE.get('paused'):
                    STATE['paused'] = True
                    STATE['pausedAt'] = time.time()
                    REVISION += 1
                    persist()
            elif STATE.get('paused'):
                DEADLINE += max(0, time.time()-STATE.get('pausedAt',time.time()))
                STATE['paused'] = False
                STATE.pop('pausedAt',None)
                REVISION += 1
                persist()
            return jsonify(checkpoint())
        if action == 'close-round':
            if j.get('id') != STATE.get('id'):
                return err('الجولة غير متاحة',409)
            if STATE.get('active'):
                finish_round('أنهى القائد الجولة، بلا نقاط')
            return jsonify(checkpoint())
        if action == 'sync':
            game = j.get('game')
            if not isinstance(game,dict):
                return err('حالة غير صالحة',400)
            snapshot = j.get('snapshot')
            if not isinstance(snapshot,dict) or type(j.get('revision')) is not int:
                return err('حدّث لوحة القائد قبل المتابعة',409)
            if j['revision'] != REVISION:
                return jsonify(error='تم تحديث المباراة من تبويب آخر',conflict=True,**checkpoint()),409
            if game.get('active') and (not isinstance(snapshot.get('answer'),str) or snapshot.get('id')!=game.get('id') or not isinstance(snapshot.get('points'),dict) or not isinstance(snapshot.get('settings'),dict)):
                return err('حالة السؤال غير صالحة',400)
            if game.get('active'):
                try:
                    assert set(snapshot['points']) == {'a','b'} and all(type(v) is int and v>=0 for v in snapshot['points'].values())
                    assert snapshot.get('stage') in (0,1,2,3,4) and snapshot.get('running') is True
                    assert all(1<=int(snapshot['settings'][k])<=999999 for k in ('reward','target'))
                    assert norm(snapshot['answer']) and isinstance(game.get('id'),str) and game['id']
                except (AssertionError,ValueError,TypeError,KeyError):
                    return err('إعدادات الجولة أو النقاط غير صالحة',400)
            if game.get('active') and (not STATE.get('active') or game['id'] != STATE.get('id')) and not online_player_count():
                return jsonify(error='لازم يكون فيه لاعب واحد على الأقل مقبول ومتصل لبدء الجولة.', startBlocked=True, **checkpoint()),409
            for key in ('teamHints','hintRequests','freezeUsed','frozen','freezeNote'):
                snapshot.pop(key,None)
                if MATCH and game.get('id') and game.get('id')==STATE.get('id') and MATCH.get(key):
                    snapshot[key] = MATCH[key]
            aliases = snapshot.get('aliases', [])
            if not isinstance(aliases,list) or len(aliases)>12 or any(not isinstance(v,str) or not norm(v) or len(v)>45 for v in aliases):
                return err('بدائل الإجابة غير صالحة',400)
            base = {k:v for k,v in MATCH.items() if k!='log'} if isinstance(MATCH,dict) else MATCH
            client_log = snapshot.pop('log',None)
            old_log = client_log if isinstance(client_log,list) and all(isinstance(x,str) and len(x)<=200 for x in client_log) and len(client_log)<=80 else ((MATCH or {}).get('log') or [])
            if snapshot.get('stage') == -1:
                old_log = []
                STATS.clear()
            changed = snapshot != base
            mutate_log = False
            if changed:
                MATCH = snapshot
                if old_log:
                    MATCH['log'] = old_log
                REVISION += 1
            elif isinstance(MATCH,dict) and MATCH.get('log') != old_log:
                MATCH['log'] = old_log
                mutate_log = True
            clock = (game.get('id'),bool(game.get('active')))
            if clock[1] and (not STATE.get('active') or clock[0]!=STATE.get('id')):
                left=j.get('leftMs',0)
                DEADLINE=time.time()+min(300000,max(0,left if type(left) in (int,float) else 0))/1000
            elif not clock[1]:
                DEADLINE=0
            SOLUTION=norm(snapshot['answer']) if clock[1] else ''
            reaction = STATE.get('roundReaction')
            round_winner = STATE.get('roundWinner')
            prevPaused, prevPausedAt, prevStarted, prevId = STATE.get('paused'), STATE.get('pausedAt'), STATE.get('startedAt'), STATE.get('id')
            STATE={k:game.get(k) for k in ('active','id','image','mask','status','score')}
            if prevPaused:
                STATE['paused'] = True
                STATE['pausedAt'] = prevPausedAt
            if clock[1] and clock[0] == prevId and prevStarted:
                STATE['startedAt'] = prevStarted
            elif clock[1]:
                STATE['startedAt'] = time.time()
            if reaction and clock[0] and time.time()<reaction['expires']:
                STATE['roundReaction'] = reaction
            if round_winner and clock[0] == prevId and not clock[1]:
                STATE['roundWinner'] = round_winner
            image = STATE.get('image')
            if not clock[1] or not isinstance(image,str) or not re.fullmatch(r'data:image/svg\+xml;base64,[A-Za-z0-9+/]+={0,2}',image):
                STATE['image'] = ''
            if not clock[1]:
                STATE['mask'] = ''
            if changed or mutate_log:
                persist()
            return jsonify(revision=REVISION,onlineCount=online_player_count(),roundId=clock[0],remainingMs=checkpoint()['remainingMs'],paused=bool(STATE.get('paused')),stats=stats_list(20),cardEvents=recent_cards(),players=[{k:p[k] for k in ('id','name','team','status')} | {'cards':p.get('cards',0)} for p in PLAYERS.values()])
    return err('الرابط غير متاح',404)
