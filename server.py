#!/usr/bin/env python3
"""Автомактаб: SQLite-backed school-management API; Python standard library only.
Put behind an HTTPS reverse proxy for use outside localhost.
"""
from __future__ import annotations
import argparse
import datetime as dt
import getpass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from contextlib import contextmanager
import re
import secrets
import sqlite3
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(BASE / 'data')))
DB = DATA_DIR / 'avtomaktab.sqlite3'
PUBLIC = BASE  # assets live at repository root
ROLES = {'director', 'accountant', 'cashier', 'teacher'}
PERMS = {
 'director': {'students_write', 'groups_write', 'payments_read', 'payments_write', 'payments_void', 'attendance_write', 'reports', 'backup', 'users', 'audit'},
 'accountant': {'payments_read', 'payments_write', 'payments_void', 'reports'},
 'cashier': {'payments_read', 'payments_write'},
 'teacher': {'attendance_write'},
}
STATUS_STUDENT = {'Ўқимоқда', 'Тугатган', 'Тўхтатилган'}
STATUS_GROUP = {'Фаол', 'Якунланган'}
STATUS_ATT = {'Келди', 'Келмади', 'Сабабли'}
METHODS = {'Нақд', 'Карта', 'Банк'}
SESSION_SECONDS = 12 * 3600
MAX_JSON_BYTES = 1024 * 1024
PASSWORD_ITERATIONS = 390_000
LOG = logging.getLogger('avtomaktab')
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, category TEXT NOT NULL,
 teacher TEXT NOT NULL, start_date TEXT NOT NULL, days TEXT NOT NULL,
 time TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0),
 fee INTEGER NOT NULL CHECK(fee>=0), status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
 group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
 joined TEXT NOT NULL, fee INTEGER NOT NULL CHECK(fee>=0), status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_students_group ON students(group_id);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL, group_id TEXT REFERENCES groups(id) ON DELETE SET NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS payments (
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
 paid_on TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
 method TEXT NOT NULL, note TEXT NOT NULL, voided INTEGER NOT NULL DEFAULT 0,
 created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL,
 voided_by TEXT REFERENCES users(id), voided_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_payments_student ON payments(student_id);
CREATE TABLE IF NOT EXISTS attendance (
 student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 on_date TEXT NOT NULL, status TEXT NOT NULL,
 updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL,
 PRIMARY KEY(student_id,group_id,on_date)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
 user_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL,
 object_id TEXT, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 username TEXT NOT NULL, client_ip TEXT NOT NULL, happened_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_recent ON login_attempts(username,client_ip,happened_at);
"""


# Render Free disks are ephemeral. DATABASE_URL selects a durable external
# Supabase PostgreSQL database; local SQLite remains available for tests.
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_POSTGRES = bool(DATABASE_URL)
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
 teacher TEXT NOT NULL, start_date TEXT NOT NULL, days TEXT NOT NULL,
 time TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0),
 fee BIGINT NOT NULL CHECK(fee>=0), status TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_groups_name_lower ON groups(LOWER(name));
CREATE TABLE IF NOT EXISTS students (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
 group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
 joined TEXT NOT NULL, fee BIGINT NOT NULL CHECK(fee>=0), status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_students_group ON students(group_id);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL,
 password_hash TEXT NOT NULL, role TEXT NOT NULL,
 group_id TEXT REFERENCES groups(id) ON DELETE SET NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_name_lower ON users(LOWER(username));
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf TEXT NOT NULL, expires_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS payments (
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
 paid_on TEXT NOT NULL, amount BIGINT NOT NULL CHECK(amount>0),
 method TEXT NOT NULL, note TEXT NOT NULL, voided INTEGER NOT NULL DEFAULT 0,
 created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL,
 voided_by TEXT REFERENCES users(id), voided_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_payments_student ON payments(student_id);
CREATE TABLE IF NOT EXISTS attendance (
 student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 on_date TEXT NOT NULL, status TEXT NOT NULL,
 updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL,
 PRIMARY KEY(student_id,group_id,on_date)
);
CREATE TABLE IF NOT EXISTS audit (
 id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, at TEXT NOT NULL,
 user_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL,
 object_id TEXT, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 username TEXT NOT NULL, client_ip TEXT NOT NULL, happened_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_recent ON login_attempts(username,client_ip,happened_at);
"""

class HybridRow(dict):
    """Preserve SQLite Row's string and integer access over psycopg dict rows."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)

class PgResult:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
    def fetchone(self):
        row = self.cursor.fetchone()
        return HybridRow(row) if row is not None else None
    def fetchall(self):
        return [HybridRow(row) for row in self.cursor.fetchall()]
    def __iter__(self):
        return (HybridRow(row) for row in self.cursor)

class PgConnection:
    def __init__(self, db_url):
        import psycopg
        from psycopg.rows import dict_row
        # Supabase session pooler URL should include sslmode=require.
        self.connection = psycopg.connect(db_url, autocommit=True,
                                          connect_timeout=12, row_factory=dict_row)
    def execute(self, sql, parameters=()):
        sql = sql.strip()
        if sql.upper().startswith('PRAGMA '):
            return None
        if sql.upper() == 'BEGIN IMMEDIATE':
            sql = 'BEGIN'
        # Our SQL is parameterised with positional question marks; no SQL
        # string literals in this application contain question marks.
        sql = sql.replace('?', '%s').replace('role="director"', "role='director'")
        cur = self.connection.execute(sql, parameters)
        return PgResult(cur)
    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)
    def commit(self):
        self.connection.commit()
    def rollback(self):
        self.connection.rollback()
    def close(self):
        self.connection.close()
    def __enter__(self):
        return self
    def __exit__(self, typ, value, traceback):
        if typ: self.rollback()
        else: self.commit()
        self.close()

class ApiError(Exception):
 def __init__(self, message, code=400):
  super().__init__(message); self.code = code

def now(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
def uid(): return uuid.uuid4().hex
def db_connect():
 if USE_POSTGRES:
  return PgConnection(DATABASE_URL)
 DATA_DIR.mkdir(parents=True, exist_ok=True)
 c=sqlite3.connect(DB, timeout=15, isolation_level=None)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA foreign_keys=ON')
 c.execute('PRAGMA busy_timeout=15000')
 return c

@contextmanager
def managed_db():
 c=db_connect()
 try:
  yield c
  c.commit()
 except Exception:
  c.rollback()
  raise
 finally:c.close()

def init_db():
 DATA_DIR.mkdir(parents=True, exist_ok=True)
 with managed_db() as c:
  if not USE_POSTGRES: c.execute('PRAGMA journal_mode=WAL')
  c.executescript(POSTGRES_SCHEMA if USE_POSTGRES else SCHEMA)

def pwd_hash(password):
 salt=secrets.token_bytes(16)
 hashed=hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), salt, PASSWORD_ITERATIONS)
 return f'pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${hashed.hex()}'

def check_pwd(password, stored):
 try:
  method, iters, salt, hashed=stored.split('$')
  if method!='pbkdf2_sha256': return False
  calc=hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), bytes.fromhex(salt), int(iters))
  return hmac.compare_digest(calc,bytes.fromhex(hashed))
 except (ValueError, TypeError): return False

def valid_password(p):
 if not isinstance(p,str) or not 12 <= len(p) <= 128: raise ApiError('Парол камида 12 та белги бўлиши керак.')
 if not any(c.isalpha() for c in p) or not any(c.isdigit() for c in p): raise ApiError('Паролда ҳарф ва рақам бўлиши керак.')
 return p

def vstr(obj,key,maxlen=140,required=True):
 val=obj.get(key,'')
 if not isinstance(val,str): raise ApiError(f'{key}: матн талаб қилинади.')
 val=val.strip()
 if (required and not val) or len(val)>maxlen or '\x00' in val: raise ApiError(f'{key}: нотўғри қиймат.')
 return val

def vdate(obj,key):
 val=vstr(obj,key,10)
 try:
  parsed=dt.date.fromisoformat(val)
  if parsed.isoformat()!=val: raise ValueError()
 except ValueError: raise ApiError(f'{key}: сана нотўғри.')
 if not (2020 <= parsed.year <= 2120): raise ApiError(f'{key}: сана чегарадан ташқари.')
 return val

def vint(obj,key,lower=0,upper=1_000_000_000_000):
 val=obj.get(key)
 if isinstance(val,bool) or not isinstance(val,(int,str)) or not re.fullmatch(r'[0-9]{1,14}',str(val)):
  raise ApiError(f'{key}: мусбат бутун сон керак.')
 v=int(val)
 if v<lower or v>upper: raise ApiError(f'{key}: чегарадан ташқари.')
 return v

def venum(obj,key,choices):
 v=vstr(obj,key,50)
 if v not in choices: raise ApiError(f'{key}: танланган қиймат қабул қилинмади.')
 return v

def vtime(obj,key):
 val=vstr(obj,key,5)
 if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',val): raise ApiError('Вақт HH:MM бўлиши керак.')
 return val

def audit(c, u, action, kind, oid='', details=''):
 c.execute('INSERT INTO audit(at,user_id,actor,action,kind,object_id,details) VALUES(?,?,?,?,?,?,?)',
  (now(),u['id'] if u else None,u['username'] if u else 'console',action,kind,oid,str(details)[:500]))

def group_fields(x):
 return {'name':vstr(x,'name',100), 'category':vstr(x,'category',50),
 'teacher':vstr(x,'teacher',120), 'start_date':vdate(x,'start'),
 'days':vstr(x,'days',120), 'time':vtime(x,'time'),
 'capacity':vint(x,'capacity',1,500), 'fee':vint(x,'fee'),
 'status':venum(x,'status', STATUS_GROUP)}

def student_fields(x):
 return {'name':vstr(x,'name',140), 'phone':vstr(x,'phone',40),
 'group_id':vstr(x,'groupId',60), 'joined':vdate(x,'join'),
 'fee':vint(x,'fee'), 'status':venum(x,'status', STATUS_STUDENT)}

def as_state(c, u):
 role=u['role']; gid=u['group_id']
 if role=='teacher':
  gs=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchall() if gid else []
  ss=c.execute('SELECT * FROM students WHERE group_id=?',(gid,)).fetchall() if gid else []
  attendance=c.execute('SELECT * FROM attendance WHERE group_id=? AND student_id IN (SELECT id FROM students WHERE group_id=?)',(gid,gid)).fetchall() if gid else []
 else:
  gs=c.execute('SELECT * FROM groups ORDER BY name').fetchall()
  ss=c.execute('SELECT * FROM students ORDER BY joined DESC').fetchall()
  attendance=c.execute('SELECT * FROM attendance').fetchall() if role in ('director','accountant') else []
 pays=c.execute('SELECT * FROM payments WHERE voided=0 ORDER BY paid_on DESC, created_at DESC').fetchall() if 'payments_read' in PERMS[role] else []
 return {
  'groups':[{'id':g['id'],'name':g['name'],'category':g['category'],'teacher':g['teacher'],'start':g['start_date'],
     'days':g['days'],'time':g['time'],'capacity':g['capacity'],'fee':g['fee'] if role!='teacher' else 0,'status':g['status']} for g in gs],
  'students':[{'id':s['id'],'name':s['name'],'phone':s['phone'] if role!='teacher' else '',
    'groupId':s['group_id'],'join':s['joined'],'fee':s['fee'] if role!='teacher' else 0,'status':s['status']} for s in ss],
  'payments':[{'id':p['id'],'studentId':p['student_id'],'date':p['paid_on'],'amount':p['amount'],'method':p['method'],'note':p['note']} for p in pays],
  'attendance':[{'studentId':a['student_id'],'groupId':a['group_id'],'date':a['on_date'],'status':a['status']} for a in attendance]
 }

def user_info(u):
 return {'id':u['id'],'username':u['username'],'role':u['role'],'groupId':u['group_id'], 'permissions':sorted(PERMS[u['role']])}

def user_by_session(c, token):
 if not token or len(token)>130: return None
 h=hashlib.sha256(token.encode()).hexdigest()
 return c.execute('''SELECT users.*,sessions.csrf,sessions.expires_at FROM sessions
 JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=?
 AND sessions.expires_at>? AND users.active=1''', (h,int(time.time()))).fetchone()

def require_perm(u, permission):
 if permission not in PERMS[u['role']]: raise ApiError('Бу амал учун рухсат йўқ.',403)

def transactional(c,fn):
 c.execute('BEGIN IMMEDIATE')
 try:
  answer=fn()
  c.execute('COMMIT')
  return answer
 except Exception:
  c.execute('ROLLBACK')
  raise

def api_action(c,u,method,path,x):
 # Permission checks are enforced on the server. UI hiding is not security.
 if path=='/api/groups' and method=='POST' or path.startswith('/api/groups/') and method in ('PUT','DELETE'):
  require_perm(u,'groups_write')
  gid=path.rsplit('/',1)[-1] if path!='/api/groups' else uid()
  def task():
   if method=='DELETE':
    if c.execute('SELECT 1 FROM students WHERE group_id=?',(gid,)).fetchone(): raise ApiError('Гуруҳда ўқувчилар бор.')
    if c.execute('SELECT 1 FROM users WHERE group_id=?',(gid,)).fetchone(): raise ApiError('Ўқитувчи аккаунти ушбу гуруҳга боғланган.')
    if c.execute('SELECT 1 FROM attendance WHERE group_id=?',(gid,)).fetchone(): raise ApiError('Давомат тарихи бор гуруҳни ўчириб бўлмайди.')
    n=c.execute('DELETE FROM groups WHERE id=?',(gid,)).rowcount
    if not n: raise ApiError('Гуруҳ топилмади.',404)
    audit(c,u,'delete','group',gid)
   else:
    fields=group_fields(x)
    if method=='PUT':
     c.execute('SELECT id FROM groups WHERE id=? FOR UPDATE',(gid,)) if USE_POSTGRES else None
     count=c.execute('SELECT COUNT(*) FROM students WHERE group_id=?',(gid,)).fetchone()[0]
     if fields['capacity']<count: raise ApiError('Гуруҳ сиғими ўқувчилар сонидан кам.')
     sets=', '.join(k+'=?' for k in fields)
     n=c.execute(f'UPDATE groups SET {sets} WHERE id=?',(*fields.values(),gid)).rowcount
     if not n: raise ApiError('Гуруҳ топилмади.',404)
    else:
     c.execute('INSERT INTO groups(id,'+','.join(fields)+') VALUES(?,?,?,?,?,?,?,?,?,?)',(gid,*fields.values()))
    audit(c,u,method.lower(),'group',gid,fields['name'])
   return {'ok':True,'id':gid}
  return transactional(c,task)
 if path=='/api/students' and method=='POST' or path.startswith('/api/students/') and method in ('PUT','DELETE'):
  require_perm(u,'students_write')
  sid=path.rsplit('/',1)[-1] if path!='/api/students' else uid()
  def task():
   if method=='DELETE':
    if c.execute('SELECT 1 FROM payments WHERE student_id=?',(sid,)).fetchone(): raise ApiError('Тўлов тарихи бор ўқувчини ўчириб бўлмайди.')
    if c.execute('SELECT 1 FROM attendance WHERE student_id=?',(sid,)).fetchone(): raise ApiError('Давомат тарихи бор ўқувчини ўчириб бўлмайди.')
    n=c.execute('DELETE FROM students WHERE id=?',(sid,)).rowcount
    if not n: raise ApiError('Ўқувчи топилмади.',404)
    audit(c,u,'delete','student',sid)
   else:
    fields=student_fields(x)
    if method=='PUT' and USE_POSTGRES:
     # Serialize fee changes with concurrent payment creation.
     c.execute('SELECT id FROM students WHERE id=? FOR UPDATE',(sid,)).fetchone()
    g=c.execute('SELECT capacity FROM groups WHERE id=?'+(' FOR UPDATE' if USE_POSTGRES else ''),(fields['group_id'],)).fetchone()
    if not g: raise ApiError('Гуруҳ топилмади.')
    n=c.execute('SELECT COUNT(*) FROM students WHERE group_id=? AND id<>?',(fields['group_id'],sid)).fetchone()[0]
    if n>=g['capacity']: raise ApiError('Гуруҳ тўлган.')
    paid=c.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE student_id=? AND voided=0',(sid,)).fetchone()[0]
    if fields['fee']<paid: raise ApiError('Шартнома тўланган суммадан кам бўлиши мумкин эмас.')
    if method=='PUT':
     sets=', '.join(k+'=?' for k in fields)
     n=c.execute(f'UPDATE students SET {sets} WHERE id=?',(*fields.values(),sid)).rowcount
     if not n: raise ApiError('Ўқувчи топилмади.',404)
    else:
     c.execute('INSERT INTO students(id,'+','.join(fields)+') VALUES(?,?,?,?,?,?,?)',(sid,*fields.values()))
    audit(c,u,method.lower(),'student',sid,fields['name'])
   return {'ok':True,'id':sid}
  return transactional(c,task)
 if path=='/api/payments' and method=='POST':
  require_perm(u,'payments_write')
  sid=vstr(x,'studentId',60); amount=vint(x,'amount',1)
  date=vdate(x,'date'); payment_method=venum(x,'method',METHODS); note=vstr(x,'note',240,False)
  def task():
   row=c.execute('SELECT fee FROM students WHERE id=?'+(' FOR UPDATE' if USE_POSTGRES else ''),(sid,)).fetchone()
   if not row: raise ApiError('Ўқувчи топилмади.',404)
   paid=c.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE student_id=? AND voided=0',(sid,)).fetchone()[0]
   if amount>row['fee']-paid: raise ApiError('Тўлов қолган қарздан ошиб кетди.')
   pid=uid()
   c.execute('INSERT INTO payments(id,student_id,paid_on,amount,method,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',
      (pid,sid,date,amount,payment_method,note,u['id'],now()))
   audit(c,u,'create','payment',pid,f'{amount} UZS; student {sid}')
   return {'ok':True,'id':pid}
  return transactional(c,task)
 if re.fullmatch(r'/api/payments/[a-f0-9]{32}/void',path) and method=='POST':
  require_perm(u,'payments_void'); pid=path.split('/')[3]
  def task():
   p=c.execute('SELECT * FROM payments WHERE id=? AND voided=0',(pid,)).fetchone()
   if not p: raise ApiError('Тўлов топилмади ёки бекор қилинган.',404)
   c.execute('UPDATE payments SET voided=1,voided_by=?,voided_at=? WHERE id=?',(u['id'],now(),pid))
   audit(c,u,'void','payment',pid,f'{p["amount"]} UZS; student {p["student_id"]}')
   return {'ok':True}
  return transactional(c,task)
 if path=='/api/attendance/upsert' and method=='POST':
  require_perm(u,'attendance_write')
  gid=vstr(x,'groupId',60); date=vdate(x,'date'); items=x.get('entries')
  if not isinstance(items,list) or len(items)>500: raise ApiError('Давомат рўйхати нотўғри.')
  if u['role']=='teacher' and gid!=u['group_id']: raise ApiError('Бошқа гуруҳ давоматини таҳрирлаш мумкин эмас.',403)
  if len({t.get('studentId') for t in items if isinstance(t,dict)})!=len(items): raise ApiError('Ўқувчи такрорланди.')
  for item in items:
   if not isinstance(item,dict): raise ApiError('Нотўғри давомат сатри.')
   vstr(item,'studentId',60);venum(item,'status',STATUS_ATT)
  def task():
   if not c.execute('SELECT 1 FROM groups WHERE id=?',(gid,)).fetchone(): raise ApiError('Гуруҳ топилмади.')
   for item in items:
    sid=item['studentId'];status=item['status']
    if not c.execute('SELECT 1 FROM students WHERE id=? AND group_id=?',(sid,gid)).fetchone(): raise ApiError('Ўқувчи танланган гуруҳда эмас.')
    c.execute('''INSERT INTO attendance(student_id,group_id,on_date,status,updated_by,updated_at)
     VALUES(?,?,?,?,?,?) ON CONFLICT(student_id,group_id,on_date)
     DO UPDATE SET status=excluded.status,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
     (sid,gid,date,status,u['id'],now()))
   audit(c,u,'upsert','attendance',gid,f'{date}; {len(items)} students')
   return {'ok':True,'saved':len(items)}
  return transactional(c,task)
 if path=='/api/users' and method=='POST':
  require_perm(u,'users')
  username=vstr(x,'username',45).lower(); password=valid_password(x.get('password'))
  role=venum(x,'role',ROLES); gid=x.get('groupId') or None
  if not re.fullmatch(r'[a-z0-9_.-]{3,45}',username): raise ApiError('Логин: 3–45 та лотин ҳарфи, рақам ёки ._-')
  def task():
   if role=='teacher':
    if not gid or not c.execute('SELECT 1 FROM groups WHERE id=?',(gid,)).fetchone(): raise ApiError('Ўқитувчи учун гуруҳ танлансин.')
   else: gid_local=None
   i=uid(); c.execute('INSERT INTO users(id,username,password_hash,role,group_id,active,created_at) VALUES(?,?,?,?,?,1,?)',
     (i,username,pwd_hash(password),role,gid if role=='teacher' else None,now()))
   audit(c,u,'create','user',i,username+' '+role)
   return {'ok':True,'id':i}
  return transactional(c,task)
 if re.fullmatch(r'/api/users/[a-f0-9]{32}/disable',path) and method=='POST':
  require_perm(u,'users'); tid=path.split('/')[3]
  if tid==u['id']: raise ApiError('Ўз аккаунтингизни ўчириб бўлмайди.')
  def task():
   t=c.execute('SELECT * FROM users WHERE id=?',(tid,)).fetchone()
   if not t: raise ApiError('Фойдаланувчи топилмади.',404)
   if t['role']=='director' and c.execute("SELECT COUNT(*) FROM users WHERE role='director' AND active=1").fetchone()[0]<=1:
    raise ApiError('Охирги фаол директорни ўчириш мумкин эмас.')
   c.execute('UPDATE users SET active=0 WHERE id=?',(tid,));c.execute('DELETE FROM sessions WHERE user_id=?',(tid,))
   audit(c,u,'disable','user',tid,t['username'])
   return {'ok':True}
  return transactional(c,task)
 if re.fullmatch(r'/api/users/[a-f0-9]{32}/enable',path) and method=='POST':
  require_perm(u,'users');tid=path.split('/')[3]
  def task():
   t=c.execute('SELECT username FROM users WHERE id=?',(tid,)).fetchone()
   if not t: raise ApiError('Фойдаланувчи топилмади.',404)
   c.execute('UPDATE users SET active=1 WHERE id=?',(tid,))
   audit(c,u,'enable','user',tid,t['username'])
   return {'ok':True}
  return transactional(c,task)
 if re.fullmatch(r'/api/users/[a-f0-9]{32}/reset-password',path) and method=='POST':
  require_perm(u,'users');tid=path.split('/')[3];password=valid_password(x.get('password'))
  def task():
   t=c.execute('SELECT username FROM users WHERE id=?',(tid,)).fetchone()
   if not t: raise ApiError('Фойдаланувчи топилмади.',404)
   c.execute('UPDATE users SET password_hash=? WHERE id=?',(pwd_hash(password),tid))
   c.execute('DELETE FROM sessions WHERE user_id=?',(tid,))
   audit(c,u,'reset-password','user',tid,t['username'])
   return {'ok':True}
  return transactional(c,task)
 if path=='/api/change-password' and method=='POST':
  old=x.get('oldPassword');password=valid_password(x.get('newPassword'))
  if not isinstance(old,str) or not check_pwd(old,u['password_hash']): raise ApiError('Эски парол нотўғри.',403)
  if hmac.compare_digest(old,password): raise ApiError('Янги парол эскисидан фарқ қилиши керак.')
  def task():
   c.execute('UPDATE users SET password_hash=? WHERE id=?',(pwd_hash(password),u['id']))
   c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
   audit(c,u,'change-password','user',u['id'])
   return {'ok':True,'relogin':True}
  return transactional(c,task)
 raise ApiError('Манзил ёки усул нотўғри.',404)

class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 server_version='Avtomaktab/1.0'
 def log_message(self,format,*args):
  LOG.info('%s %s',self.address_string(),format%args)
 def headers_common(self, content_type='application/json; charset=utf-8', length=0, status=200, cookie=None):
  self.send_response(status)
  self.send_header('Content-Type',content_type)
  self.send_header('Content-Length',str(length))
  self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') or content_type.startswith('text/html') else 'public, max-age=3600')
  self.send_header('X-Content-Type-Options','nosniff')
  self.send_header('X-Frame-Options','DENY')
  self.send_header('Referrer-Policy','no-referrer')
  self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
  self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
  if os.environ.get('HTTPS_ONLY','0')=='1': self.send_header('Strict-Transport-Security','max-age=31536000')
  if cookie is not None: self.send_header('Set-Cookie',cookie)
  self.end_headers()
 def send_json(self,doc,status=200,cookie=None):
  b=json.dumps(doc,ensure_ascii=False,default=str).encode('utf-8')
  self.headers_common(length=len(b),status=status,cookie=cookie)
  self.wfile.write(b)
 def send_file(self,filename,mime):
  path=PUBLIC / filename
  if not path.is_file(): raise ApiError('Файл топилмади.',404)
  b=path.read_bytes()
  self.headers_common(mime,len(b)); self.wfile.write(b)
 def request_json(self):
  try: n=int(self.headers.get('Content-Length','0'))
  except ValueError: raise ApiError('Content-Length нотўғри.')
  if n<=0 or n>MAX_JSON_BYTES: raise ApiError('JSON ҳажми 1 МБдан ошмаслиги керак.',413)
  if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json': raise ApiError('Content-Type application/json бўлиши керак.',415)
  raw=self.rfile.read(n)
  try: x=json.loads(raw)
  except (UnicodeDecodeError,json.JSONDecodeError): raise ApiError('JSON формати нотўғри.')
  if not isinstance(x,dict): raise ApiError('JSON объект бўлиши керак.')
  return x
 def session_token(self):
  try:
   cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
   return cookie['avt_session'].value if 'avt_session' in cookie else None
  except Exception: return None
 def new_cookie(self, token='', clear=False):
  secure='; Secure' if os.environ.get('HTTPS_ONLY','0')=='1' else ''
  return 'avt_session='+token+'; Path=/; HttpOnly; SameSite=Lax'+secure+('; Max-Age=0' if clear else '; Max-Age='+str(SESSION_SECONDS))
 def serve(self):
  path=urlsplit(self.path).path
  method=self.command
  if not path.startswith('/api/'):
   if method!='GET': raise ApiError('Манзил топилмади.',404)
   if path=='/': return self.send_file('index.html','text/html; charset=utf-8')
   if path=='/app.js': return self.send_file('app.js','text/javascript; charset=utf-8')
   if path=='/style.css': return self.send_file('style.css','text/css; charset=utf-8')
   if path=='/favicon.ico':
    self.headers_common('text/plain',0,status=204);return
   raise ApiError('Манзил топилмади.',404)
  if path=='/api/health' and method=='GET':
   with managed_db() as check:
    check.execute('SELECT 1 AS ok').fetchone()
   return self.send_json({'ok':True})
  c=db_connect()
  try:
   c.execute('DELETE FROM sessions WHERE expires_at<?',(int(time.time()),)) if secrets.randbelow(100)==0 else None
   if path=='/api/login' and method=='POST':
    x=self.request_json()
    username=vstr(x,'username',45).lower();pw=x.get('password')
    if not isinstance(pw,str) or len(pw)>256: raise ApiError('Логин ёки парол нотўғри.',401)
    ip=self.client_address[0][:64];t=int(time.time())
    c.execute('DELETE FROM login_attempts WHERE happened_at<?',(t-1800,))
    tries=c.execute('SELECT COUNT(*) FROM login_attempts WHERE username=? AND client_ip=? AND happened_at>?',(username,ip,t-900)).fetchone()[0]
    if tries>=7: raise ApiError('Уринишлар сони ошди. 15 дақиқадан кейин қайта урининг.',429)
    target=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
    if not target or not check_pwd(pw,target['password_hash']):
     c.execute('INSERT INTO login_attempts VALUES(?,?,?)',(username,ip,t))
     raise ApiError('Логин ёки парол нотўғри.',401)
    c.execute('DELETE FROM login_attempts WHERE username=? AND client_ip=?',(username,ip))
    token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(32)
    c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)',
       (hashlib.sha256(token.encode()).hexdigest(),target['id'],csrf,t+SESSION_SECONDS))
    audit(c,target,'login','auth')
    return self.send_json({'user':user_info(target),'csrf':csrf},cookie=self.new_cookie(token))
   u=user_by_session(c,self.session_token())
   if not u: raise ApiError('Сессия тугаган. Қайта киринг.',401)
   if method!='GET':
    csrf=self.headers.get('X-CSRF-Token','')
    if not csrf or not hmac.compare_digest(csrf,u['csrf']): raise ApiError('Ҳимоя токени нотўғри.',403)
    origin=self.headers.get('Origin')
    if origin:
     orig=urlsplit(origin);host=self.headers.get('Host','')
     if not (orig.netloc==host and orig.scheme in ('http','https')): raise ApiError('Ташқи манбадан сўров тақиқланган.',403)
   if path=='/api/me' and method=='GET': return self.send_json({'user':user_info(u),'csrf':u['csrf']})
   if path=='/api/state' and method=='GET': return self.send_json(as_state(c,u))
   if path=='/api/users' and method=='GET':
    require_perm(u,'users')
    rows=c.execute('SELECT id,username,role,group_id,active,created_at FROM users ORDER BY created_at DESC').fetchall()
    return self.send_json({'users':[{'id':z['id'],'username':z['username'],'role':z['role'],'groupId':z['group_id'],'active':bool(z['active']),'createdAt':z['created_at']} for z in rows]})
   if path=='/api/audit' and method=='GET':
    require_perm(u,'audit')
    return self.send_json({'rows':[dict(z) for z in c.execute('SELECT at,actor,action,kind,object_id,details FROM audit ORDER BY id DESC LIMIT 100')]})
   if path=='/api/backup' and method=='GET':
    require_perm(u,'backup')
    result=as_state(c,u);result.update({'version':2,'exportedAt':now(),'note':'Data-only backup, accounts and passwords excluded.'})
    audit(c,u,'export','backup')
    return self.send_json(result)
   if path=='/api/logout' and method=='POST':
    c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(self.session_token().encode()).hexdigest(),))
    audit(c,u,'logout','auth')
    return self.send_json({'ok':True},cookie=self.new_cookie(clear=True))
   if method in ('POST','PUT','DELETE'):
    x=self.request_json() if method!='DELETE' else {}
    return self.send_json(api_action(c,u,method,path,x))
   raise ApiError('Манзил ёки усул нотўғри.',404)
  finally:c.close()
 def _dispatch(self):
  try:self.serve()
  except ApiError as e:self.send_json({'error':str(e)},e.code)
  except sqlite3.IntegrityError as e:
   LOG.warning('Database integrity error: %s',e)
   self.send_json({'error':'Маълумот такрорланган ёки боғлиқ ёзув мавжуд.'},409)
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception as err:
   if USE_POSTGRES:
    import psycopg
    if isinstance(err, psycopg.IntegrityError):
     LOG.warning('Postgres integrity error: %s',type(err).__name__)
     return self.send_json({'error':'Маълумот такрорланган ёки боғлиқ ёзув мавжуд.'},409)
   LOG.exception('Unexpected error')
   try:self.send_json({'error':'Серверда хатолик. Администраторга мурожаат қилинг.'},500)
   except (BrokenPipeError,ConnectionResetError):pass
 def do_GET(self):self._dispatch()
 def do_POST(self):self._dispatch()
 def do_PUT(self):self._dispatch()
 def do_DELETE(self):self._dispatch()

def sqlite_backup():
 if USE_POSTGRES:
  raise RuntimeError('PostgreSQL захираси Supabase dashboard орқали ёки директорнинг JSON экспорти орқали олинади.')
 DATA_DIR.mkdir(parents=True,exist_ok=True)
 dest=DATA_DIR/'backups';dest.mkdir(exist_ok=True)
 name=dest/('avtomaktab_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')+'.sqlite3')
 source=db_connect()
 try:
  out=sqlite3.connect(name)
  try:source.backup(out)
  finally:out.close()
 finally:source.close()
 for old in sorted(dest.glob('avtomaktab_*.sqlite3'))[:-30]: old.unlink()
 LOG.info('Database backup: %s',name)
 return name

def start_backup_job():
 def loop():
  while True:
   time.sleep(24*3600)
   try:sqlite_backup()
   except Exception:LOG.exception('Backup failed')
 threading.Thread(target=loop,daemon=True,name='backup-scheduler').start()

def do_init_admin(username):
 username=username.strip().lower()
 if not re.fullmatch(r'[a-z0-9_.-]{3,45}',username): raise SystemExit('Логин: 3–45 та лотин ҳарфи, рақам, ._-')
 password=os.environ.get('ADMIN_PASSWORD') or getpass.getpass('Янги директор пароли (камида 12 белги, ҳарф ва рақам): ')
 try:valid_password(password)
 except ApiError as e:raise SystemExit(str(e))
 with managed_db() as c:
  if c.execute('SELECT 1 FROM users WHERE role="director"').fetchone(): raise SystemExit('Директор аккаунти мавжуд. Аввал у билан тизимга киринг.')
  c.execute('INSERT INTO users(id,username,password_hash,role,group_id,created_at) VALUES(?,?,?,?,?,?)',
      (uid(),username,pwd_hash(password),'director',None,now()))
 print(f'Директор аккаунти яратилди: {username} (парол файлга ёзилмади).')

def import_demo(filename):
 blob=json.loads(Path(filename).read_text(encoding='utf-8'))
 data=blob.get('data',blob)
 if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in ('groups','students','payments','attendance')):
  raise SystemExit('DEMO JSON формати нотўғри.')
 with managed_db() as c:
  def task():
   if c.execute('SELECT COUNT(*) FROM students').fetchone()[0] or c.execute('SELECT COUNT(*) FROM groups').fetchone()[0]:
    raise ApiError('Импорт фақат бўш базага мумкин.')
   idsG=set();idsS=set();idsP=set()
   for g in data['groups']:
    f=group_fields(g);gid=vstr(g,'id',60)
    if gid in idsG:raise ApiError('Гуруҳ ID такрорланган')
    idsG.add(gid)
    c.execute('INSERT INTO groups(id,'+','.join(f)+') VALUES(?,?,?,?,?,?,?,?,?,?)',(gid,*f.values()))
   for s in data['students']:
    f=student_fields(s);sid=vstr(s,'id',60)
    if sid in idsS:raise ApiError('Ўқувчи ID такрорланган')
    idsS.add(sid)
    c.execute('INSERT INTO students(id,'+','.join(f)+') VALUES(?,?,?,?,?,?,?)',(sid,*f.values()))
   for p in data['payments']:
    pid=vstr(p,'id',60);sid=vstr(p,'studentId',60)
    if pid in idsP:raise ApiError('Тўлов ID такрорланган')
    idsP.add(pid)
    amount=vint(p,'amount',1);on=vdate(p,'date');meth=venum(p,'method',METHODS)
    c.execute('INSERT INTO payments(id,student_id,paid_on,amount,method,note,created_at) VALUES(?,?,?,?,?,?,?)',
      (pid,sid,on,amount,meth,vstr(p,'note',240,False),now()))
   for a in data['attendance']:
    c.execute('INSERT INTO attendance(student_id,group_id,on_date,status,updated_at) VALUES(?,?,?,?,?)',
      (vstr(a,'studentId',60),vstr(a,'groupId',60),vdate(a,'date'),venum(a,'status',STATUS_ATT),now()))
   for s in data['students']:
    sid=s['id'];total=c.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE student_id=?',(sid,)).fetchone()[0]
    if total>s['fee']:raise ApiError('Импортда шартнома суммасидан ортиқ тўлов бор.')
   audit(c,None,'import','demo',details=f"{len(idsS)} students")
   return None
  transactional(c,task)
 print('DEMO маълумотлари марказий базага импорт қилинди.')

def main():
 parser=argparse.ArgumentParser(description='Автомактаб бошқарув тизими')
 sub=parser.add_subparsers(dest='cmd')
 sub.add_parser('serve');adm=sub.add_parser('init-admin');adm.add_argument('--username',default='admin')
 backup_cmd=sub.add_parser('backup');imp=sub.add_parser('import-demo');imp.add_argument('file')
 args=parser.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
 if os.environ.get('REQUIRE_POSTGRES','0')=='1' and not USE_POSTGRES:
  raise SystemExit('DATABASE_URL йўқ: Render учун Supabase PostgreSQL уланиши МАЖБУРИЙ. SQLite билан ишга туширилмайди.')
 init_db()
 if args.cmd=='init-admin':return do_init_admin(args.username)
 if args.cmd=='backup':return print('Захира файли:',sqlite_backup())
 if args.cmd=='import-demo':return import_demo(args.file)
 with managed_db() as c:
  director_exists=bool(c.execute('SELECT 1 FROM users WHERE role="director"').fetchone())
 if not director_exists:
  # Bootstrap on first deployment; NEVER ship credentials in the source.
  # The secret must be configured securely in Railway's Variables UI.
  bootstrap=os.environ.pop('BOOTSTRAP_ADMIN_PASSWORD',None)
  if not bootstrap:
   raise SystemExit('Биринчи ишга тушириш учун BOOTSTRAP_ADMIN_PASSWORD ни сервернинг махфий variables бўлимига киритинг (камида 12 белги).')
  username=os.environ.get('BOOTSTRAP_ADMIN_USERNAME','admin')
  previous=os.environ.get('ADMIN_PASSWORD')
  os.environ['ADMIN_PASSWORD']=bootstrap
  try:do_init_admin(username)
  finally:
   if previous is None:os.environ.pop('ADMIN_PASSWORD',None)
   else:os.environ['ADMIN_PASSWORD']=previous
 if not USE_POSTGRES:
  sqlite_backup()
  start_backup_job()
 host=os.environ.get('HOST','127.0.0.1');port=int(os.environ.get('PORT','8080'))
 httpd=ThreadingHTTPServer((host,port),Handler)
 print(f'АВТОМАКТАБ: http://{host}:{port} (HTTPS учун reverse proxy керак)')
 try:httpd.serve_forever()
 except KeyboardInterrupt:pass
 finally:httpd.server_close()

if __name__=='__main__':main()
