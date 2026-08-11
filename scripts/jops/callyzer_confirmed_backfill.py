#!/usr/bin/env python3
"""Approved Callyzer backfill: confirmed matches + definitely-unmatched calls only."""
import datetime as dt, json, re, subprocess, sys, time, urllib.error, urllib.request, uuid
from collections import Counter
SCHEMA='workspace_1l3urgumjmspnjxohclmfz6fx'; API='https://api1.callyzer.co/api/v2.2/call-log/history'; IST=dt.timezone(dt.timedelta(hours=5,minutes=30)); PAGE=99; BUFFER=120; TOKEN_SOURCE='/opt/jops/callyzer_recording_sync.py'
def token():
 m=re.search(r'CALYZER_TOKEN\s*=\s*["\']([^"\']+)',open(TOKEN_SOURCE).read()); return m.group(1)
def sqlstr(v):
 if v is None:return 'NULL'
 return "E'"+str(v).replace('\\','\\\\').replace("'","''").replace('\n','\\n')+"'"
def psql(sql,timeout=180):
 r=subprocess.run(['docker','exec','-i','twenty-db-1','psql','-U','twenty','-d','default','-t','-A','-F','|'],input=sql,text=True,capture_output=True,timeout=timeout)
 if r.returncode: raise RuntimeError(r.stderr[:1000])
 return r.stdout
def fetch(method,start,end,tok):
 out=[]; page=1
 while True:
  body=json.dumps({'synced_from':start,'synced_to':end,'page_no':page,'page_size':PAGE,'call_method':method,'call_mode':'Voice'})
  for attempt in range(6):
   if page>1 or attempt: time.sleep(2.2)
   req=urllib.request.Request(API,data=body.encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+tok})
   try:
    with urllib.request.urlopen(req,timeout=90) as r:d=json.load(r); break
   except urllib.error.HTTPError as e:
    if e.code not in (429,500,502,503,504): raise
    time.sleep(3+attempt*2)
  rows=d.get('result') or []; out+=rows
  if len(rows)<PAGE or len(out)>=int(d.get('total_records') or 0): return out
  page+=1
def digits(x):return ''.join(c for c in str(x or '') if c.isdigit())[-10:]
def direction(x):return {'Incoming':'INBOUND','Outgoing':'OUTBOUND','Missed':'MISSED','Rejected':'REJECTED'}.get(x,x).upper()
def ts(c):
 return dt.datetime.strptime(c['call_date']+' '+c['call_time'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=IST).timestamp()
def main():
 now=dt.datetime.now(IST); start=int(dt.datetime.combine(dt.date(2026,8,1),dt.time.min,IST).timestamp()); end=int(now.timestamp()); tok=token(); source={}
 # Fetch one IST day at a time. This avoids provider 400s on a large historical request.
 day=dt.datetime.combine(dt.date(2026,8,1),dt.time.min,IST)
 while day < now:
  day_end=min(day+dt.timedelta(days=1),now); ds=int(day.timestamp()); de=int(day_end.timestamp())
  for m in ('PhoneCall','WhatsAppCall'):
   for c in fetch(m,ds,de,tok):
    if c.get('id'): source[c['id']]={**c,'method':'PHONE' if m=='PhoneCall' else 'WHATSAPP'}
  day=day+dt.timedelta(days=1)
 vals=','.join(sqlstr(x) for x in source)
 q=f'''SELECT c.id,c."timestamp",c.duration,c.direction,COALESCE(wm."officePhonePrimaryPhoneNumber",''),COALESCE(p."phonesPrimaryPhoneNumber",''),COALESCE(c."messageId",'') FROM "{SCHEMA}"."_communication" c LEFT JOIN "{SCHEMA}"."workspaceMember" wm ON wm.id=c."assignedagentId" LEFT JOIN "{SCHEMA}".person p ON p.id=c."personId" WHERE c."communicationType"='CALL' AND c."deletedAt" IS NULL AND c."timestamp">=to_timestamp({start}) AND c."timestamp"<to_timestamp({end});'''
 crm=[]
 for line in psql(q).splitlines():
  p=line.split('|')
  if len(p)>=7:
   try: crm.append({'id':p[0],'ts':dt.datetime.fromisoformat(p[1].replace('Z','+00:00')).timestamp(),'dur':int(float(p[2] or 0)),'dir':p[3],'emp':digits(p[4]),'client':digits(p[5]),'msg':p[6]})
   except: pass
 used=set(); matches=[]; ambiguous=[]; unmatched=[]; byid=set(x['msg'] for x in crm if x['msg'])
 for cid,c in source.items():
  if cid in byid: continue
  try: ct=ts(c)
  except: unmatched.append((cid,c)); continue
  cand=[x for x in crm if x['emp']==digits(c.get('emp_number')) and x['client']==digits(c.get('client_number')) and x['dir']==direction(c.get('call_type')) and abs(x['ts']-ct)<=BUFFER and x['id'] not in used]
  if len(cand)==1: used.add(cand[0]['id']); matches.append((c,cand[0]))
  elif len(cand)>1: ambiguous.append(c)
  else: unmatched.append((cid,c))
 # Existing matched rows: write ID and method only.
 updates=[]
 for c,x in matches: updates.append(f'''UPDATE "{SCHEMA}"."_communication" SET "messageId"={sqlstr(c['id'])},"callMethod"='{c['method']}',"updatedAt"=NOW() WHERE id={sqlstr(x['id'])} AND "messageId" IS NULL;''')
 # Resolve/create persons for definitely-unmatched calls.
 miss=[c for _,c in unmatched]; phones=sorted({digits(c.get('client_number')) for c in miss if digits(c.get('client_number'))}); emps=sorted({digits(c.get('emp_number')) for c in miss if digits(c.get('emp_number'))})
 def inlist(xs):return ','.join(sqlstr(x) for x in xs) or 'NULL'
 persons={}; members={}
 for l in psql(f'''SELECT id,"phonesPrimaryPhoneNumber" FROM "{SCHEMA}".person WHERE "deletedAt" IS NULL AND "phonesPrimaryPhoneNumber" IN ({inlist(phones)});''').splitlines():
  p=l.split('|',1)
  if len(p)==2:persons[digits(p[1])]=p[0]
 for l in psql(f'''SELECT id,"officePhonePrimaryPhoneNumber" FROM "{SCHEMA}"."workspaceMember" WHERE "deletedAt" IS NULL AND "officePhonePrimaryPhoneNumber" IN ({inlist(emps)});''').splitlines():
  p=l.split('|',1)
  if len(p)==2:members[digits(p[1])]=p[0]
 creates=[]
 for ph in phones:
  if ph not in persons: creates.append(f'''INSERT INTO "{SCHEMA}".person ("id","createdAt","updatedAt","deletedAt","nameFirstName","nameLastName","phonesPrimaryPhoneNumber","phonesPrimaryPhoneCountryCode","phonesPrimaryPhoneCallingCode") SELECT {sqlstr(str(uuid.uuid4()))},NOW(),NOW(),NULL,'undefined',NULL,{sqlstr(ph)},'IN','+91' WHERE NOT EXISTS (SELECT 1 FROM "{SCHEMA}".person WHERE "deletedAt" IS NULL AND "phonesPrimaryPhoneNumber"={sqlstr(ph)});''')
 if creates:
  sql='BEGIN;\n'+'\n'.join(creates)+'\nCOMMIT;'; open('/tmp/callyzer_people.sql','w').write(sql); subprocess.run(['docker','cp','/tmp/callyzer_people.sql','twenty-db-1:/tmp/callyzer_people.sql'],check=True); subprocess.run(['docker','exec','twenty-db-1','psql','-U','twenty','-d','default','-f','/tmp/callyzer_people.sql'],check=True,capture_output=True)
  for l in psql(f'''SELECT id,"phonesPrimaryPhoneNumber" FROM "{SCHEMA}".person WHERE "deletedAt" IS NULL AND "phonesPrimaryPhoneNumber" IN ({inlist(phones)});''').splitlines():
   p=l.split('|',1)
   if len(p)==2:persons[digits(p[1])]=p[0]
 inserts=[]
 for cid,c in unmatched:
  try: timestamp=dt.datetime.fromtimestamp(ts(c),dt.timezone.utc).isoformat().replace('+00:00','Z')
  except: continue
  raw=direction(c.get('call_type')) or 'OUTBOUND'; dur=int(float(c.get('duration') or 0)); url=(c.get('call_recording_url') or '').strip(); name=f"📞{(c.get('client_name') or 'Unknown').strip() or 'Unknown'} x {(c.get('emp_name') or 'Unknown').strip() or 'Unknown'} - {c.get('call_time') or ''}"
  inserts.append(f'''INSERT INTO "{SCHEMA}"."_communication" ("id","createdAt","updatedAt","deletedAt","communicationType",duration,"timestamp",summary,name,"createdBySource","createdByName","updatedBySource","updatedByName","personId",direction,"callLinkPrimaryLinkUrl","callLinkPrimaryLinkLabel","messageId","assignedagentId","callMethod",position) SELECT {sqlstr(str(uuid.uuid4()))},NOW(),NOW(),NULL,'CALL',{dur},{sqlstr(timestamp)},{sqlstr('Call recording: '+url if url else 'No call recording available for this log.')},{sqlstr(name)},'WORKFLOW','Callyzer VPS backfill','WORKFLOW','Callyzer VPS backfill',{sqlstr(persons.get(digits(c.get('client_number'))))},'{raw}',{sqlstr(url)},'Call Recording',{sqlstr(cid)},{sqlstr(members.get(digits(c.get('emp_number'))))},'{c['method']}',0 WHERE NOT EXISTS (SELECT 1 FROM "{SCHEMA}"."_communication" WHERE "communicationType"='CALL' AND "deletedAt" IS NULL AND "messageId"={sqlstr(cid)});''')
 allsql=updates+inserts
 if allsql:
  sql='BEGIN;\n'+'\n'.join(allsql)+'\nCOMMIT;'; open('/tmp/callyzer_backfill.sql','w').write(sql); subprocess.run(['docker','cp','/tmp/callyzer_backfill.sql','twenty-db-1:/tmp/callyzer_backfill.sql'],check=True); subprocess.run(['docker','exec','twenty-db-1','psql','-U','twenty','-d','default','-f','/tmp/callyzer_backfill.sql'],check=True,capture_output=True)
 print(json.dumps({'window':'2026-08-01 IST to now','source_unique':len(source),'confirmed_id_updates':len(updates),'ambiguous_skipped':len(ambiguous),'definitely_unmatched_backfilled':len(inserts),'placeholder_persons_created':len(creates),'no_writes':False},separators=(',',':')))
if __name__=='__main__': main()
