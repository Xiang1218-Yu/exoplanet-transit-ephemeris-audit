import hashlib,json,math,os,subprocess,sys,tempfile
from pathlib import Path
PROGRAM=Path(os.environ.get('PROGRAM_DIR','/app'))/'transit_audit.py'
T0=2459000.0

def scene(**changes):
 x={'candidate':{'id':'EPX-17','reference_midpoint_bjd':T0,'period_days':2.0,'duration_minutes':120.0,'reference_sigma_minutes':2.0,'drift_sigma_minutes_per_epoch':1.5},'observations':[{'id':'v1','start_bjd':T0-1/48,'end_bjd':T0+1/48,'quality':'CLEAR','airmass':1.2}],'policy':{'min_coverage_fraction':0.5,'max_airmass':2.0,'require_clear':False,'max_ephemeris_sigma_minutes':10.0}}
 x.update(changes);return x

def invoke(payload, report=b'old-report\n', ledger=b'{\"candidate_id\":\"prior\",\"fingerprint\":\"old\",\"qualified_epochs\":0,\"status\":\"READY\"}\n', reorder=False):
 with tempfile.TemporaryDirectory() as raw:
  d=Path(raw); inp=d/'in.json'; out=d/'report.json'; log=d/'ledger.jsonl'; inp.write_text(json.dumps(payload,sort_keys=reorder),encoding='utf-8');out.write_bytes(report);log.write_bytes(ledger)
  p=subprocess.run([sys.executable,str(PROGRAM),'--input',str(inp),'--output',str(out),'--ledger',str(log)],capture_output=True,text=True)
  return p,out.read_bytes() if out.exists() and out.is_file() else None,log.read_bytes() if log.exists() and log.is_file() else None,sorted(v.name for v in d.iterdir())
def successful(payload):
 p,out,_,_=invoke(payload,b'',b'');assert p.returncode==0,p.stderr;return json.loads(out),out
def rejected(payload):
 p,out,log,files=invoke(payload);assert p.returncode!=0;assert p.stderr.splitlines()[0].startswith('REJECT ');assert out==b'old-report\n' and log==b'{\"candidate_id\":\"prior\",\"fingerprint\":\"old\",\"qualified_epochs\":0,\"status\":\"READY\"}\n';assert files==['in.json','ledger.jsonl','report.json']

def test_epoch_enumeration_and_partial_coverage():
 """R2/R3：每个相交历元均应列出，并按凌星区间计算部分覆盖。"""
 x=scene(observations=[{'id':'late','start_bjd':T0+2-1/24,'end_bjd':T0+2,'quality':'CLEAR','airmass':1.1},{'id':'core','start_bjd':T0-1/48,'end_bjd':T0+1/48,'quality':'CLEAR','airmass':1.1}])
 d,_=successful(x);assert [v['epoch'] for v in d['epochs']]==[0,1];assert d['epochs'][0]['coverage_fraction']==0.5 and d['epochs'][0]['status']=='COVERED';assert d['epochs'][1]['coverage_fraction']==0.5

def test_negative_epoch_and_touching_boundaries_are_not_candidates():
 """R2：负历元可出现；仅端点相接的观测不构成候选。"""
 x=scene(observations=[{'id':'neg','start_bjd':T0-2-1/96,'end_bjd':T0-2+1/96,'quality':'CLEAR','airmass':1.1},{'id':'touch','start_bjd':T0+1/24,'end_bjd':T0+1/12,'quality':'CLEAR','airmass':1.1}])
 d,_=successful(x);assert [v['epoch'] for v in d['epochs']]==[-1];assert d['epochs'][0]['sigma_minutes']==2.5

def test_overlapping_visits_use_union_not_sum():
 """R3：同一凌星的重叠曝光按并集而非重复时长计入。"""
 x=scene(observations=[{'id':'a','start_bjd':T0-1/24,'end_bjd':T0,'quality':'CLEAR','airmass':1.1},{'id':'b','start_bjd':T0-1/96,'end_bjd':T0+1/48,'quality':'CLEAR','airmass':1.1}])
 d,_=successful(x);assert d['epochs'][0]['coverage_fraction']==0.75;assert d['epochs'][0]['qualifying_visit_ids']==['a','b']

def test_quality_airmass_and_clear_policy_control_coverage():
 """R3：质量政策和气团上限共同决定哪些观测可计入。"""
 visits=[{'id':'thin','start_bjd':T0-1/24,'end_bjd':T0,'quality':'THIN','airmass':1.1},{'id':'high','start_bjd':T0,'end_bjd':T0+1/24,'quality':'CLEAR','airmass':2.5}]
 d,_=successful(scene(observations=visits));assert d['epochs'][0]['coverage_fraction']==0.5 and d['epochs'][0]['qualifying_visit_ids']==['thin']
 d,_=successful(scene(observations=visits,policy={'min_coverage_fraction':.5,'max_airmass':2.0,'require_clear':True,'max_ephemeris_sigma_minutes':10.0}));assert d['epochs'][0]['coverage_fraction']==0.0 and d['epochs'][0]['status']=='LOW_COVERAGE'

def test_sigma_is_quadrature_and_threshold_is_inclusive():
 """R4：历表不确定度按平方和开方，并在相等上限时接受。"""
 x=scene(observations=[{'id':'e1','start_bjd':T0+2-1/24,'end_bjd':T0+2+1/24,'quality':'CLEAR','airmass':1.1}],policy={'min_coverage_fraction':.999,'max_airmass':2.0,'require_clear':False,'max_ephemeris_sigma_minutes':2.5})
 d,_=successful(x);assert d['epochs'][0]['epoch']==1 and d['epochs'][0]['sigma_minutes']==2.5 and d['epochs'][0]['status']=='COVERED'

def test_review_has_priority_over_ready():
 """R4：只要有覆盖达标但历表超限的历元，顶层状态必须复核。"""
 visits=[{'id':'zero','start_bjd':T0-1/24,'end_bjd':T0+1/24,'quality':'CLEAR','airmass':1.1},{'id':'one','start_bjd':T0+2-1/24,'end_bjd':T0+2+1/24,'quality':'CLEAR','airmass':1.1}]
 d,_=successful(scene(observations=visits,policy={'min_coverage_fraction':.999,'max_airmass':2.0,'require_clear':False,'max_ephemeris_sigma_minutes':2.1}));assert [v['status'] for v in d['epochs']]==['COVERED','TIMING_REVIEW'];assert d['status']=='REVIEW'

def test_no_transit_and_insufficient_are_distinct():
 """R4：无相交历元和有候选但覆盖不足必须给出不同结论。"""
 d,_=successful(scene(observations=[{'id':'far','start_bjd':T0+.2,'end_bjd':T0+.3,'quality':'CLEAR','airmass':1.1}]));assert d['status']=='NO_TRANSIT' and d['epochs']==[]
 d,_=successful(scene(observations=[{'id':'tiny','start_bjd':T0-1/480,'end_bjd':T0+1/480,'quality':'CLEAR','airmass':1.1}]));assert d['status']=='INSUFFICIENT' and d['epochs'][0]['status']=='LOW_COVERAGE'

def test_report_contract_is_compact_sorted_rounded_and_stable():
 """R5：成功报告必须满足精确字段、紧凑排序、LF 与稳定数值契约。"""
 d,raw=successful(scene());assert set(d)=={'candidate_id','epochs','status','summary'};assert set(d['epochs'][0])=={'coverage_fraction','epoch','midpoint_bjd','qualifying_visit_ids','sigma_minutes','status'};assert raw==json.dumps(d,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()+b'\n';assert b', ' not in raw and raw.endswith(b'\n')

def test_canonical_ledger_is_idempotent_across_key_order():
 """R6：相同规范输入即使源键顺序不同，账本也只能保留一条事件。"""
 x=scene()
 with tempfile.TemporaryDirectory() as raw:
  d=Path(raw); inp=d/'in.json';out=d/'out.json';log=d/'ledger.jsonl';cmd=[sys.executable,str(PROGRAM),'--input',str(inp),'--output',str(out),'--ledger',str(log)]
  inp.write_text(json.dumps(x),encoding='utf-8');assert subprocess.run(cmd,capture_output=True,text=True).returncode==0;first=log.read_bytes()
  inp.write_text(json.dumps(x,sort_keys=True),encoding='utf-8');assert subprocess.run(cmd,capture_output=True,text=True).returncode==0;assert log.read_bytes()==first
  event=json.loads(first);assert event['fingerprint']==hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest();assert set(event)=={'candidate_id','fingerprint','qualified_epochs','status'}

def test_distinct_valid_input_appends_one_ledger_event():
 """R6：不同规范输入成功后账本应只新增一个确定事件。"""
 with tempfile.TemporaryDirectory() as raw:
  d=Path(raw);inp=d/'in.json';out=d/'out.json';log=d/'ledger.jsonl';cmd=[sys.executable,str(PROGRAM),'--input',str(inp),'--output',str(out),'--ledger',str(log)]
  for x in (scene(),scene(candidate={'id':'EPX-18','reference_midpoint_bjd':T0,'period_days':2.0,'duration_minutes':120.0,'reference_sigma_minutes':2.0,'drift_sigma_minutes_per_epoch':1.5})):
   inp.write_text(json.dumps(x),encoding='utf-8');assert subprocess.run(cmd,capture_output=True,text=True).returncode==0
  rows=[json.loads(v) for v in log.read_text(encoding='utf-8').splitlines()];assert len(rows)==2 and {v['candidate_id'] for v in rows}=={'EPX-17','EPX-18'}

def test_malformed_ledger_rejection_preserves_outputs_and_directory():
 """R1/R6：无法读取既有账本时，两个目标和目录都保持不变。"""
 p,out,log,files=invoke(scene(),b'prior report',b'not-json\n');assert p.returncode!=0 and p.stderr.splitlines()[0]=='REJECT LEDGER_READ';assert out==b'prior report' and log==b'not-json\n';assert files==['in.json','ledger.jsonl','report.json']

def test_duplicate_observation_id_rejection_preserves_outputs():
 """R1：同一输入中的重复观测 ID 必须被拒收且不改写已有对象。"""
 x=scene();x['observations'].append(dict(x['observations'][0]));rejected(x)

def test_schema_type_and_range_rejection_preserves_outputs():
 """R1：布尔伪数值和越界值必须被拒收且不改写已有对象。"""
 x=scene();x['candidate']['period_days']=True;rejected(x)
 x=scene();x['observations'][0]['airmass']=3.1;rejected(x)

def test_success_has_no_temp_artifacts_and_output_target_error_is_rejected():
 """R5/R6：成功不残留临时文件；无效输出目标必须不污染账本。"""
 with tempfile.TemporaryDirectory() as raw:
  d=Path(raw);inp=d/'in.json';out=d/'out.json';log=d/'ledger.jsonl';inp.write_text(json.dumps(scene()),encoding='utf-8');cmd=[sys.executable,str(PROGRAM),'--input',str(inp),'--output',str(out),'--ledger',str(log)]
  assert subprocess.run(cmd,capture_output=True,text=True).returncode==0;assert not [v for v in d.iterdir() if v.name.startswith('.transit-audit-')]
  before=log.read_bytes();bad=d/'bad';bad.mkdir();(bad/'keep.txt').write_bytes(b'keep');before_bad=sorted((v.name,v.read_bytes()) for v in bad.iterdir());p=subprocess.run([sys.executable,str(PROGRAM),'--input',str(inp),'--output',str(bad),'--ledger',str(log)],capture_output=True,text=True);assert p.returncode!=0 and p.stderr.splitlines()[0]=='REJECT IO';assert log.read_bytes()==before and bad.is_dir() and sorted((v.name,v.read_bytes()) for v in bad.iterdir())==before_bad;assert not [v for v in d.iterdir() if v.name.startswith('.transit-audit-')]
