#!/usr/bin/env python3
"""Audit predicted transit visits."""
import argparse, hashlib, json, math, os, sys
from pathlib import Path

def reject(code):
    print(f"REJECT {code}", file=sys.stderr); raise SystemExit(2)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--output',required=True); ap.add_argument('--ledger',required=True); a=ap.parse_args()
    try: data=json.loads(Path(a.input).read_text(encoding='utf-8'))
    except Exception: reject('INPUT')
    try:
        c=data['candidate']; p=data['policy']; visits=data['observations']
        duration=c['duration_minutes']/1440; epoch=round((visits[0]['start_bjd']-c['reference_midpoint_bjd'])/c['period_days'])
        midpoint=c['reference_midpoint_bjd']+epoch*c['period_days']
        overlap=max(0.0,min(visits[0]['end_bjd'],midpoint+duration/2)-max(visits[0]['start_bjd'],midpoint-duration/2))
        coverage=overlap/duration
        sigma=c['reference_sigma_minutes']+abs(epoch)*c['drift_sigma_minutes_per_epoch']
        good=visits[0]['quality']!='CLOUD' and visits[0]['airmass']<=p['max_airmass']
        state='COVERED' if good and coverage>=p['min_coverage_fraction'] and sigma<=p['max_ephemeris_sigma_minutes'] else 'LOW_COVERAGE'
        report={'candidate_id':c['id'],'epochs':[{'coverage_fraction':round(coverage,6),'epoch':epoch,'midpoint_bjd':round(midpoint,8),'qualifying_visit_ids':[visits[0]['id']] if good else [],'sigma_minutes':round(sigma,6),'status':state}],'status':'READY' if state=='COVERED' else 'INSUFFICIENT','summary':{'candidate_epochs':1,'covered_epochs':int(state=='COVERED'),'timing_review_epochs':0}}
        raw=json.dumps(report,sort_keys=True,separators=(',',':'))+'\n'; Path(a.output).write_text(raw,encoding='utf-8')
        fp=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        Path(a.ledger).write_text(json.dumps({'candidate_id':c['id'],'fingerprint':fp,'qualified_epochs':int(state=='COVERED'),'status':report['status']},sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')
    except Exception: reject('SCHEMA')
if __name__=='__main__': main()
