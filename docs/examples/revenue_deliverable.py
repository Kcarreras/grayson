"""Example authoring recipe, not a required template or chart grammar.

Pass a successfully executed query returning MONTH, ACTUAL and PLAN in month
order. Those numeric values are in currency units; aggregations belong in SQL.
Use build_presentation(qid) as session_deliverable's presentation argument.
"""


def build_presentation(qid: str) -> dict:
    return {
        "summary": (
            "# Revenue trajectory\n\nExplore monthly actual revenue against plan. "
            "Use the month selector to inspect individual observations and the plan toggle "
            "to compare trajectories. Values are taken from the cited SQL result; "
            "this example supplies no business conclusion."
        ),
        "datasets": {"revenue": {"qid": qid, "columns": ["MONTH", "ACTUAL", "PLAN"]}},
        "visuals": {
            "trajectory": {
                "title": "Revenue trajectory and selected month",
                "datasets": ["revenue"],
                "note": "Month selector highlights one source row. Plan can be hidden.",
            },
        },
        "html": """
<main>
 <div class="eyebrow">PERFORMANCE NOTE / INTERACTIVE EXPLORER</div>
 <div class="intro"><div><h1>The revenue<br>trajectory.</h1>
 <p>Follow actual performance against the plan.<br>
 Every point comes from the attached SQL evidence.</p></div>
 <div class="stamp">SQL-backed inputs<br><strong>Agent-designed view</strong></div></div>
 <div class="layout"><section class="chart-card">
 <div class="chart-head"><h2>Monthly revenue</h2>
 <label><input id="plan" type="checkbox" checked> Show plan</label></div>
 <div class="legend"><span>● Actual</span><span>― Plan</span></div>
 <svg id="trajectory" viewBox="0 0 800 350" role="img"
 aria-label="Monthly actual revenue and plan"></svg>
 <label class="scrub">Explore a month <input id="month" type="range" min="0" value="0"></label>
 </section><aside><div class="eyebrow">SELECTED OBSERVATION</div><h2 id="selected"></h2>
 <div class="metric"><span>Actual revenue</span><strong id="actual"></strong></div>
 <div class="metric"><span>Planned revenue</span><strong id="planned"></strong></div>
 <p id="source" class="source"></p></aside></div>
 <footer>Use the controls to inspect the evidence. The chart does not establish causality.</footer>
</main>""",
        "css": """
:root{font:16px/1.5 system-ui;color:#182d32;background:#f4f2ec}
*{box-sizing:border-box}body{margin:0}main{max-width:1260px;margin:auto;padding:48px}
.eyebrow{font-size:11px;letter-spacing:.17em;font-weight:700;color:#517071}
.intro{display:flex;justify-content:space-between;align-items:center;margin:22px 0 36px}
h1{font-family:Georgia,serif;font-size:62px;line-height:1.04;letter-spacing:-2px;margin:0 0 20px}
.intro p{color:#587073}.stamp{border-top:3px solid #137d76;padding:18px 0;font-size:12px}
.layout{display:grid;grid-template-columns:3fr 1fr;gap:24px}.chart-card{background:white;
border:1px solid #dde2de;border-radius:18px;padding:24px}
.chart-head{display:flex;justify-content:space-between;align-items:center}h2{font-size:20px}
label{font-size:13px}.legend{display:flex;gap:20px;color:#167c75;font-size:12px}
.legend span+span{color:#919f9f}svg{display:block;width:100%;overflow:visible}
.scrub{display:flex;gap:18px;align-items:center}input[type=range]{flex:1;accent-color:#137d76}
aside{background:#173d40;color:#e8f3ee;border-radius:18px;padding:28px}
aside .eyebrow{color:#99c3bc}.metric{border-top:1px solid #416163;padding:22px 0}
.metric span{font-size:13px;color:#b3ccca}
.metric strong{display:block;font-size:30px;margin-top:8px}
.source{font:11px/1.8 ui-monospace,monospace;overflow-wrap:anywhere;color:#a7c8c2}
footer{font-size:12px;color:#657c7a;margin-top:24px}
@media(max-width:800px){main{padding:24px}.layout{grid-template-columns:1fr}
h1{font-size:44px}.stamp{display:none}.intro{margin-bottom:24px}}
""",
        "javascript": """
const rows = grayson.data('revenue');
const svg = document.getElementById('trajectory');
const slider = document.getElementById('month');
const toggle = document.getElementById('plan');
const money = value => new Intl.NumberFormat('en-GB', {
  style:'currency', currency:'GBP', maximumFractionDigits:0
}).format(value);
const add = (tag, attrs, text) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key,value));
  if (text !== undefined) node.textContent = text;
  svg.append(node); return node;
};
slider.max = Math.max(0, rows.length - 1); slider.value = slider.max;
function draw() {
  svg.replaceChildren();
  if (!rows.length) {
    add('text', {x:80,y:100,fill:'#517071'}, 'No observations returned.');
    slider.disabled = true; return;
  }
  const chosen = Number(slider.value), row = rows[chosen];
  const values = rows.flatMap(r => toggle.checked ? [r.ACTUAL,r.PLAN] : [r.ACTUAL]);
  if (!values.every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0)) {
    add('text', {x:80,y:100,fill:'#a33'}, 'This example requires nonnegative numeric values.');
    return;
  }
  const maximum = Math.max(1, ...values) * 1.1;
  const x = i => 90 + i * 650 / Math.max(1, rows.length-1);
  const y = v => 280 - v / maximum * 245;
  for (let step=0; step<=4; step++) {
    const value=maximum*step/4, height=y(value);
    add('line', {x1:90,x2:740,y1:height,y2:height,stroke:'#e6ece9'});
    add('text', {x:78,y:height+4,'text-anchor':'end',fill:'#768987','font-size':11}, money(value));
  }
  rows.forEach((r,i) => {
    if (i===0 || i===rows.length-1 || i===chosen)
      add('text', {x:x(i),y:312,'text-anchor':'middle',fill:'#587073','font-size':12}, r.MONTH);
  });
  if(toggle.checked) add('polyline', {points:rows.map((r,i)=>`${x(i)},${y(r.PLAN)}`).join(' '),
    fill:'none',stroke:'#99aaa8','stroke-width':2,'stroke-dasharray':'5 6'});
  add('polyline', {points:rows.map((r,i)=>`${x(i)},${y(r.ACTUAL)}`).join(' '),
    fill:'none',stroke:'#167c75','stroke-width':3,'stroke-linejoin':'round'});
  add('line', {x1:x(chosen),x2:x(chosen),y1:30,y2:280,stroke:'#adcac4','stroke-dasharray':'3 5'});
  rows.forEach((r,i)=>{
    const dot=add('circle', {cx:x(i),cy:y(r.ACTUAL),r:i===chosen?7:4,
      fill:i===chosen?'#e6b760':'#167c75',stroke:'white','stroke-width':2});
    const tooltip=document.createElementNS('http://www.w3.org/2000/svg','title');
    tooltip.textContent=`${r.MONTH}: ${money(r.ACTUAL)}`; dot.append(tooltip);
  });
  document.getElementById('selected').textContent=row.MONTH;
  document.getElementById('actual').textContent=money(row.ACTUAL);
  document.getElementById('planned').textContent=money(row.PLAN);
  const source=grayson.evidence('revenue');
  document.getElementById('source').textContent=`Source: ${source.session_id} / ${source.qid}`;
}
slider.addEventListener('input',draw); toggle.addEventListener('change',draw); draw();
""",
    }
