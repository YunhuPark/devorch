PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DevOrch Dashboard</title>
<style>
:root{--bg:#fafafa;--fg:#1a1a1a;--mut:#666;--line:#ddd;--card:#fff;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e8e8e8;--mut:#9a9a9a;--line:#333;--card:#1f1f1f;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0}nav button{background:none;border:0;color:var(--mut);font:inherit;cursor:pointer;padding:4px 8px}
nav button.on{color:var(--fg);border-bottom:2px solid var(--fg)}
main{padding:16px;max-width:1100px;margin:auto}
table{width:100%;border-collapse:collapse;background:var(--card)}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500}tr.row{cursor:pointer}tr.row:hover{background:var(--bg)}
.tag{display:inline-block;padding:0 6px;border:1px solid var(--line);border-radius:4px;font-size:12px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}
pre{background:var(--card);border:1px solid var(--line);padding:10px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.banner{border:1px solid var(--warn);color:var(--warn);padding:6px 10px;margin:8px 0}
.scroll{overflow-x:auto}
</style></head><body>
<header><h1>DevOrch</h1><nav id="nav"></nav><span class="mut">읽기 전용 · 승인은 터미널의 <code>devorch approve</code> 로만 가능</span></header>
<main id="main">불러오는 중…</main>
<script>
const $=(t,a={},...k)=>{const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;else if(x==='onclick')e.onclick=v;else e.setAttribute(x,v)}for(const c of k.flat())e.append(c instanceof Node?c:String(c??''));return e};
const api=async p=>{const r=await fetch(p);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);return r.json()};
const main=document.getElementById('main'),nav=document.getElementById('nav');
const cls=v=>['passed','approved','READY','not_required'].includes(v)?'ok':['pending','not_run','READY_WITH_WARNINGS','stale'].includes(v)?'warn':v?'bad':'mut';
const st=v=>$('span',{class:'tag '+cls(v)},v??'-');
const tabs={tasks:'작업',audit:'감사 로그',bench:'벤치마크'};
function show(name,arg){nav.replaceChildren(...Object.entries(tabs).map(([k,l])=>$('button',{class:k===name?'on':'',onclick:()=>show(k)},l)));main.replaceChildren('불러오는 중…');
 ({tasks,audit,bench,task})[name](arg).catch(e=>main.replaceChildren($('p',{class:'bad'},'오류: '+e.message)))}
async function tasks(){const d=await api('/api/tasks');
 main.replaceChildren(d.length?$('div',{class:'scroll'},$('table',{},$('tr',{},...['작업','위험도','상태','리뷰','테스트','승인','생성'].map(h=>$('th',{},h))),
 ...d.map(t=>t.error?$('tr',{},$('td',{},t.task_id),$('td',{colspan:6,class:'bad'},t.error)):$('tr',{class:'row',onclick:()=>show('task',t.task_id)},
  $('td',{},$('code',{},t.task_id),' ',t.task),$('td',{},t.risk),$('td',{},t.state),$('td',{},st(t.gates.evaluation)),$('td',{},st(t.gates.tests)),$('td',{},st(t.gates.approval)),$('td',{class:'mut'},t.created_at))))):$('p',{class:'mut'},'작업이 없습니다. devorch mcp / run 으로 생성됩니다.'))}
async function task(id){const t=await api('/api/tasks/'+id);const ev=t.evaluation;
 main.replaceChildren($('p',{},$('button',{onclick:()=>show('tasks')},'← 목록')),$('h2',{},t.task),
  $('p',{},'위험도 ',t.risk,' (점수 ',t.risk_score,') · 상태 ',t.state,' · 리뷰어 ',t.plan.join(', ')),
  $('p',{},'게이트: 리뷰 ',st(t.gates.reviewed?t.gates.evaluation:'not_run'),' 테스트 ',st(t.gates.tests),' 승인 ',st(t.gates.approval)),
  t.approval_reasons.length?$('p',{class:'warn'},'승인 사유: '+t.approval_reasons.join('; ')):'',
  $('p',{class:'mut'},'diff ',t.diff_sha??'-',' · 브랜치 ',t.branch??'-',' · 커밋 ',t.commit??'-'),
  $('h3',{},'위험 요인'),$('pre',{},t.risk_factors.map(f=>`+${f.points}  ${f.name}: ${f.reason}`).join('\n')||'-'),
  $('h3',{},'리뷰 결과'),$('pre',{},ev?[ev.summary,...ev.blocking.map(f=>`[차단 ${f.severity}] (${f.source}) ${f.title}\n    ${f.detail}`),...ev.warnings.map(f=>`[경고 ${f.severity}] (${f.source}) ${f.title}`)].join('\n'):'아직 리뷰 없음'),
  $('h3',{},'테스트'),$('pre',{},t.test_summary||'-'),$('h3',{},'메모'),$('pre',{},t.notes.join('\n')||'-'),
  $('h3',{},'Diff'),$('pre',{},t.diff||'(없음)'))}
async function audit(){const d=await api('/api/audit?limit=200');
 main.replaceChildren(d.length?$('div',{class:'scroll'},$('table',{},$('tr',{},...['시각(UTC)','작업','이벤트','데이터'].map(h=>$('th',{},h))),
 ...d.map(e=>$('tr',{},$('td',{class:'mut'},e.ts),$('td',{},$('code',{},e.run_id)),$('td',{},e.event),$('td',{},JSON.stringify(e.data).slice(0,300)))))):$('p',{class:'mut'},'감사 로그가 없습니다.'))}
async function bench(){const d=await api('/api/bench');
 main.replaceChildren(...(d.length?d.flatMap(r=>[$('h3',{},r.id),r.mock?$('div',{class:'banner'},'MOCK — harness check only. 실제 LLM 성능이 아닙니다.'):'',$('pre',{},r.report)]):[$('p',{class:'mut'},'벤치마크 결과가 없습니다. devorch bench run 으로 생성됩니다.')]))}
show('tasks');
</script></body></html>"""
