import copy,json,re,subprocess,hashlib
from pathlib import Path
from jsonschema import Draft202012Validator

import argparse
parser=argparse.ArgumentParser(description='Offline synthetic contract checks only; makes no API calls.')
parser.add_argument('--baseline-dir',type=Path,required=True)
args=parser.parse_args()
p=Path(__file__).resolve().parent
defs=json.loads((p/'schemas/personalization.schema.json').read_text())
checks=[]
def check(name,value):
    assert value,name
    checks.append({'name':name,'result':'PASS'})
def validator(key): return Draft202012Validator({**defs,'$ref':'#/$defs/'+key})
mapping={
'02-analyze-request.json':'analyzeRequest','02-analyze-response.json':'analyzeSuccess',
'08-second-leaf-request.json':'analyzeRequest','08-second-leaf-response.json':'analyzeSuccess',
'09-merge-request.json':'mergeRequest','09-merge-response.json':'mergeSuccess',
'03-persona-request.json':'personaRequest','03-persona-response.json':'personaSuccess',
'04-no-evidence-request.json':'personaRequest','04-no-evidence-response.json':'personaSuccess'}
objects={x.name:json.loads(x.read_text()) for x in (p/'examples').glob('*.json')}
for f in (p/'schemas').glob('*.json'):
    Draft202012Validator.check_schema(json.loads(f.read_text()));check('schema:'+f.name,True)
for name,key in mapping.items():
    validator(key).validate(objects[name]);check('fixture:'+name,True)
for name,kind in [('01-baseline-request.json','request'),('01-baseline-response.json','success'),('05-busy-error.json','error'),('06-deadline-error.json','error'),('07-context-error.json','error')]:
    s=json.loads((p/f'schemas/baseline-{kind}.schema.json').read_text());Draft202012Validator(s).validate(objects[name]);check('fixture:'+name,True)
for kind in ('request','success','error'):
    check('baseline-unchanged:'+kind,(p/f'schemas/baseline-{kind}.schema.json').read_bytes()==(args.baseline_dir/f'{kind}.schema.json').read_bytes())
for prefix in ('02-analyze','08-second-leaf','09-merge'):
    req,res=(objects[prefix+'-'+x+'.json'] for x in ('request','response'))
    for key in ('request_id','persona_id','source_revision','source_snapshot_id','node_id'):
        check(prefix+':echo:'+key,req[key]==res[key])
    if prefix!='09-merge':check(prefix+':provenance',set(res['source_text_version_ids'])=={s['text_version_id'] for s in req['sources']})
merge=objects['09-merge-request.json']
for child,file in zip(merge['inputs'],('02-analyze-response.json','08-second-leaf-response.json')):
    check('merge:exact-child:'+file,child=={k:objects[file][k] for k in ('node_id','profile_content')})
check('merge:unique-distinct-parent',len({x['node_id'] for x in merge['inputs']})==len(merge['inputs']) and merge['node_id'] not in {x['node_id'] for x in merge['inputs']})
manifest=objects['10-snapshot-manifest.json']
check('snapshot:exact-cover',sorted(i for leaf in manifest['leaves'] for i in leaf['utterance_indices'])==list(range(len(manifest['utterances']))))
for reqfile,resfile,indices in [('02-analyze-request.json','02-analyze-response.json',[0,1]),('08-second-leaf-request.json','08-second-leaf-response.json',[2,3])]:
    check('snapshot:original-text:'+reqfile,objects[reqfile]['sources'][0]['target_utterances']==[manifest['utterances'][i] for i in indices])
check('chat:root-content',objects['03-persona-request.json']['style_profile']['content']==objects['09-merge-response.json']['profile_content'])
check('chat:snapshot-reference',objects['03-persona-request.json']['style_profile']['source_snapshot_id']==manifest['source_snapshot_id'])
check('chat:evidence-in-source',all(x['content'] in manifest['utterances'] for x in objects['03-persona-request.json']['evidence']))
for name,key in mapping.items():
    v=copy.deepcopy(objects[name]);v['request_id']+='\n';check('reject:uuid-newline:'+name,not validator(key).is_valid(v))
    v=copy.deepcopy(objects[name]);v['unrecognized']=True;check('reject:extra:'+name,not validator(key).is_valid(v))
for n in (0,1,9):
    v=copy.deepcopy(merge);v['inputs']=[merge['inputs'][0]]*n;check('reject:merge-count:'+str(n),not validator('mergeRequest').is_valid(v))
for name,v in objects.items():
    if name!='10-snapshot-manifest.json':check('body-cap:'+name,len(json.dumps(v,ensure_ascii=False).encode())<=262144)
for f in ('api-spec.md','api-call-flow.md'):
    text=(p/f).read_text()
    for i,code in enumerate(re.findall(r'```bash\n(.*?)```',text,re.S)):
        r=subprocess.run(['bash','-n'],input=code,text=True,capture_output=True);check('shell-syntax:'+f+':'+str(i),r.returncode==0)
    check('doc-paths:'+f,all((p/ref).exists() for ref in set(re.findall(r'(?:examples|schemas)/[\w.-]+\.json',text))))
write={'scope':'Synthetic schema/correlation/body/syntax validation only; no GPU, tokenizer, concurrency, network, DB, quality or implementation tests. Cross-field rejection and publication scenarios are requirements, not executed server tests.','checks':checks,'passed':len(checks)}
(p/'checks.json').write_text(json.dumps(write,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':len(checks)}))
