import {kitchen} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const canWrite=()=>session.user.role!=='viewer';
const isAdmin=()=>session.user.role==='admin';
const STATUS={reconciled:'已对账',pending_plan:'缺采用建议',pending_batch:'缺实领批次',missing:'整日缺失'};
const ALERT_STATUS={open:'未处理',confirmed:'已确认处理',revoked:'已撤销',superseded:'已接续'};
const EVENT_TYPE={opened:'开启预警',recalculated:'更正后重算',merged:'合并接续',split:'拆分接续',extended:'区间变化接续',confirmed:'确认处理',revoked:'撤销预警',superseded:'被新预警接续',successor_added:'补齐后继关系',correction_note:'追加更正说明'};
const NEEDS={adoption:'采用建议',batch:'实领批次'};
const OP_KEY='kitchen-operation-key';

export function newOperationId(){
 return crypto.randomUUID?crypto.randomUUID().replace(/-/g,''):Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
}
const remember=key=>sessionStorage.setItem(OP_KEY,key);

function alertCard(a,withActions=true){
 const shown=a.basis_changed&&a.latest_totals?a.latest_totals:a.totals;
 const links=[];
 if(a.predecessors.length)links.push('前驱 '+a.predecessors.map(p=>'<a href="#/kitchen/alert/'+p.id+'">'+esc(p.alert_no)+'</a>').join('、'));
 if(a.successors.length)links.push('后继 '+a.successors.map(p=>'<a href="#/kitchen/alert/'+p.id+'">'+esc(p.alert_no)+'</a>').join('、'));
 return '<article class="alert-card '+esc(a.status)+'"><div class="alert-head"><div><span class="status '+esc(a.status)+'">'+esc(ALERT_STATUS[a.status]||a.status)+'</span><strong>'+esc(a.alert_no)+'</strong><small>'+esc(a.stall)+' · '+esc(a.start_day)+' 至 '+esc(a.end_day)+' · 第 '+a.version+' 版'+(a.correction_count?' · '+a.correction_count+' 条更正说明':'')+'</small></div><a class="text-button" href="#/kitchen/alert/'+a.id+'">沿革与依据 ↗</a></div>'+
  '<p class="alert-numbers">实领 <strong>'+fmt(shown.actual??0)+'</strong> 份 · 采用建议 <strong>'+fmt(shown.adopted??0)+'</strong> 份 · 超出 <strong>'+fmt(shown.over??0)+'</strong> 份</p>'+
  (a.status==='confirmed'?'<p class="hint">确认人 '+esc(a.confirmed_by||'')+' · '+date(a.confirmed_at)+(a.confirmed_note?' · '+esc(a.confirmed_note):'')+(a.basis_changed?' · 确认后依据已更正，见更正说明':'')+'</p>':'')+
  (a.status==='revoked'&&a.revoked_reason?'<p class="hint">撤销原因：'+esc(a.revoked_reason)+'</p>':'')+
  (a.status==='superseded'&&a.superseded_reason?'<p class="hint">'+esc(a.superseded_reason)+'</p>':'')+
  (links.length?'<p class="hint">'+links.join(' · ')+'</p>':'')+
  (withActions&&a.status==='open'&&isAdmin()?'<div class="alert-actions"><button class="button small" data-confirm="'+a.id+'">确认处理</button><button class="button small" data-revoke="'+a.id+'">撤销</button></div>':'')+
 '</article>';
}

function basisTable(basis){
 if(!basis)return '<p class="empty">无依据快照。</p>';
 return '<div class="table-wrap"><table><thead><tr><th>日期</th><th>状态</th><th>建议</th><th>采用</th><th>实领</th><th>偏差(实领−建议)</th><th>批次</th></tr></thead><tbody>'+(basis.days||[]).map(d=>'<tr><td>'+esc(d.day)+'</td><td>'+esc(STATUS[d.status]||d.status)+'</td><td>'+esc(fmt(d.recommended))+'</td><td>'+esc(fmt(d.adopted))+'</td><td>'+esc(fmt(d.actual))+'</td><td>'+esc(fmt(d.deviation))+'</td><td>'+esc((d.batch_refs||[]).join('、'))+'</td></tr>').join('')+'</tbody></table></div><p class="hint">合计实领 '+fmt(basis.totals?.actual??0)+' 份，采用建议 '+fmt(basis.totals?.adopted??0)+' 份，超出 '+fmt(basis.totals?.over??0)+' 份。</p>';
}

function trailList(trail){
 const items=trail.timelines.slice(0,10);
 if(!items.length)return '<p class="empty">尚无采用或实领版本记录。</p>';
 return items.map(item=>'<article class="trail-item"><div class="trail-head"><strong>'+esc(item.day)+'</strong><small>'+esc(item.stall)+'</small></div>'+item.sources.map(source=>{
   const latest=Math.max(...source.versions.map(v=>v.revision));
   return '<div class="trail-source"><span>'+esc(source.kind==='adoption'?'采用方案':('批次 '+source.key))+'</span><ol>'+source.versions.map(v=>'<li'+(v.active?' class="active"':'')+'><b>第 '+v.revision+' 版</b> · '+v.qty+' 份'+(v.recommended!=null?'（建议 '+v.recommended+'）':'')+(v.reason?' · '+esc(v.reason):'')+(v.note?' · '+esc(v.note):'')+'<small>'+esc(v.created_by||'—')+' · '+date(v.created_at)+(v.active?' · 当前有效':' · 已由后继版本接续')+'</small></li>').join('')+'</ol></div>';
 }).join('')+'</article>').join('');
}

function forms(stall,ledger){
 return '<div class="kitchen-forms"><form id="adoption-form"><h3>登记实际采用的已批准建议</h3><label>已批准分析<select name="job_id">'+(ledger.jobs||[]).map(j=>'<option value="'+j.job_id+'">'+esc(j.name)+' · '+esc(j.from_day)+' 至 '+esc(j.to_day)+'</option>').join('')+'</select></label><label>领餐日期<input name="day" type="date" required></label><label>实际采用份数（留空按建议）<input name="qty" type="number" min="0" step="1"></label><label>调整原因（与建议不同必填）<input name="reason" maxlength="500" placeholder="例如：研发活动加班餐"></label><label>备注<input name="note" maxlength="500"></label><button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>登记采用建议</button><p class="hint">仅已完成且获批准的分析可登记；同日再次登记视为更正，旧版本留痕。</p></form>'+
 '<form id="batch-form"><h3>登记实领批次</h3><label>批次号<input name="batch_ref" required maxlength="80" placeholder="例如：PO-20261116-1"></label><label>领餐日期<input name="day" type="date" required></label><label>实领份数<input name="qty" type="number" min="0" step="1" required></label><label>备注<input name="note" maxlength="500"></label><button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>登记实领批次</button><p class="hint">同一天可有多个批次；批次号重复视为更正并生成新版本，补传后自动重算受影响区间。</p></form></div>';
}

export async function kitchenPage(stallName){
 const stalls=await kitchen.stalls();
 const stored=(()=>{try{return JSON.parse(sessionStorage.getItem('kitchen-stall')||'""')||'';}catch{return '';}})();
 const stall=stallName||stored||stalls[0]?.stall||'';
 if(!stall)return heading('06 / KITCHEN LEDGER','厨房连续少备追踪','按档口登记每日实际采用的已批准建议，与实领批次逐日对账；连续三天实领超过采用建议即开预警。')+'<section class="panel"><div class="empty">尚无可追踪的档口。先登记一天的采用建议或实领批次，系统会建立档口账本。</div><form id="stall-form"><label>档口名称<input name="stall" required maxlength="80" placeholder="例如：东港园午餐"></label><button type="submit" class="button" '+(canWrite()?'':'disabled')+'>建立档口账本</button></form></section>';
 const [ledger,pending,alerts,trail]=await Promise.all([kitchen.ledger(stall),kitchen.pending(stall),kitchen.alerts(stall),kitchen.trail(stall)]);
 const openAlerts=alerts.filter(a=>a.status==='open');
 const stallButtons=stalls.map(s=>'<button class="batch-item '+(s.stall===stall?'active':'')+'" data-stall="'+esc(s.stall)+'"><strong>'+esc(s.stall)+'</strong><span>'+s.days+' 天 · 待补齐 '+s.pending_days+' · 未处理预警 '+s.open_alerts+'</span><small>'+esc(s.from_day)+' 至 '+esc(s.to_day)+'</small></button>').join('');
 return heading('06 / KITCHEN LEDGER','厨房连续少备追踪','按档口逐日登记实际采用的已批准建议并与实领批次对账；缺日列待补齐，连续三天实领超过建议即开预警，同一连续超量段只保留一条。','<button class="button" id="refresh-kitchen">刷新账本 ↻</button>')+
  '<div class="kitchen-top"><section class="panel stall-panel"><div class="section-title"><h2>档口账本</h2><span>'+stalls.length+' 个档口</span></div><div class="batch-list">'+stallButtons+'</div><form id="stall-form"><label>切换或建立档口<input name="stall" required maxlength="80" value="'+esc(stall)+'"></label><button type="submit" class="button small" '+(canWrite()?'':'disabled')+'>打开账本</button></form></section>'+
  '<section class="panel"><div class="section-title"><h2>'+esc(stall)+' · 对账概况</h2><span>'+esc(ledger.summary.from_day||'')+' 至 '+esc(ledger.summary.to_day||'')+'</span></div><div class="kpi-strip">'+[['追踪天数',ledger.summary.tracked_days],['已对账',ledger.summary.reconciled_days],['待补齐',ledger.summary.pending_plan_days+ledger.summary.pending_batch_days+ledger.summary.missing_days],['实领超建议天数',ledger.summary.over_days]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div>'+forms(stall,ledger)+'</section></div>'+
  '<section class="panel"><div class="section-title"><h2>待补齐</h2><span>'+pending.length+' 天需要补录</span></div>'+(pending.length?'<div class="pending-grid">'+pending.map(p=>'<article><strong>'+esc(p.day)+'</strong><span class="status '+esc(p.status)+'">'+esc(STATUS[p.status])+'</span><p>缺 '+p.needs.map(n=>NEEDS[n]).join('、')+'</p><small>'+(p.recommended!=null?'建议 '+p.recommended+' 份':'尚无采用建议')+'</small>'+(canWrite()&&p.needs.includes('adoption')?'<button class="text-button" data-quick-adopt="'+esc(p.day)+'">去登记采用 ↗</button>':'')+'</article>').join('')+'</div>':'<p class="empty">全部日期已完成对账。</p>')+'</section>'+
  '<section class="panel"><div class="section-title"><h2>逐日对账与偏差</h2><span>偏差 = 实领 − 建议；采用偏差 = 实领 − 实际采用</span></div>'+table([{key:'day',label:'日期'},{key:'status',label:'状态'},{key:'recommended',label:'建议份数'},{key:'adopted',label:'采用份数'},{key:'actual',label:'实领份数'},{key:'deviation',label:'偏差(实领−建议)'},{key:'adopted_deviation',label:'偏差(实领−采用)'},{key:'batch_refs',label:'实领批次'},{key:'alerts',label:'关联预警'}],ledger.rows.map(r=>({...r,status:STATUS[r.status]||r.status,batch_refs:(r.batch_refs||[]).join('、'),alerts:(r.active_alert_nos||[]).join('、')})),'尚无逐日记录。')+'</section>'+
  '<div class="two-columns kitchen-lower"><section class="panel"><div class="section-title"><h2>预警与沿革</h2><span>'+openAlerts.length+' 条未处理 · 共 '+alerts.length+' 条版本记录</span></div>'+(alerts.length?alerts.map(alertCard).join(''):'<p class="empty">暂无预警。连续三天实领均超过采用建议时自动开启。</p>')+'</section>'+
  '<section class="panel"><div class="section-title"><h2>追溯</h2><span>采用方案、实领批次与预警处理</span></div>'+trailList(trail)+'</section></div>'+
  '<section class="panel"><div class="section-title"><h2>中断恢复</h2><span>同一操作重试不重复开预警或追加说明</span></div><form id="recover-form" class="inline-form"><label>操作编号<input name="operation_id" required minlength="8" maxlength="80" value="'+esc(sessionStorage.getItem(OP_KEY)||'')+'"></label><button type="submit" class="button" '+(canWrite()?'':'disabled')+'>重试或恢复操作</button></form><p class="hint">登记失败时页面会显示操作编号。在此重放会按编号幂等执行；中断的操作也可由后台恢复任务自动重放。</p></section>';
}

export async function alertPage(id){
 const [a]=await Promise.all([kitchen.alert(id)]);
 const basis=a.latest_basis||a.basis;
 return heading('06 / ALERT LEDGER','预警 '+a.alert_no+' 的沿革与依据',a.stall+' · '+a.start_day+' 至 '+a.end_day+' · 当前状态 '+(ALERT_STATUS[a.status]||a.status),'<a class="button" href="#/kitchen">返回厨房账本 ↗</a>')+
  '<section class="panel">'+alertCard(a,false)+
  '<div class="two-columns"><div><h3>确认时依据（保留不改）</h3>'+(a.confirmed_basis?basisTable(a.confirmed_basis):'<p class="empty">尚未确认处理。</p>')+'</div><div><h3>最新依据'+(a.basis_changed?'（确认后发生更正）':'')+'</h3>'+basisTable(basis)+'</div></div>'+
  (a.status==='open'&&isAdmin()?'<div class="alert-forms"><form id="confirm-form"><label>处理说明<input name="note" maxlength="1000" placeholder="例如：已与档口核对"></label><button type="submit" class="primary">确认按当前依据处理</button></form><form id="revoke-form"><label>撤销原因<input name="reason" required minlength="2" maxlength="1000" placeholder="仅未处理预警可撤销"></label><button type="submit" class="button">撤销预警</button></form></div>':'')+
  (a.status==='confirmed'?'<p class="hint">已确认处理，原确认依据保留；后续更正只追加说明，不覆盖确认记录。</p>':'')+
  '</section>'+
  '<section class="panel"><div class="section-title"><h2>处理与沿革记录</h2><span>'+a.events.length+' 条</span></div><ul class="timeline alert-timeline">'+a.events.map(e=>'<li><div><strong>'+esc(EVENT_TYPE[e.type]||e.type)+'</strong><span>'+esc(e.actor||'系统')+' · '+date(e.at)+' · 第 '+e.version+' 版</span>'+(e.note?'<p>'+esc(e.note)+'</p>':'')+'</div><small>'+esc(e.operation_id||'')+'</small></li>').join('')+'</ul></section>'+
  '<section class="panel"><div class="section-title"><h2>前驱与后继</h2><span>合并或拆分后保持对应关系</span></div><div class="link-grid"><div><h3>前驱</h3>'+(a.predecessors.length?a.predecessors.map(p=>'<a class="link-card" href="#/kitchen/alert/'+p.id+'">'+esc(p.alert_no)+'<span>'+esc(p.start_day)+' 至 '+esc(p.end_day)+' · '+esc(ALERT_STATUS[p.status]||p.status)+'</span></a>').join(''):'<p class="empty">无</p>')+'</div><div><h3>后继</h3>'+(a.successors.length?a.successors.map(p=>'<a class="link-card" href="#/kitchen/alert/'+p.id+'">'+esc(p.alert_no)+'<span>'+esc(p.start_day)+' 至 '+esc(p.end_day)+' · '+esc(ALERT_STATUS[p.status]||p.status)+'</span></a>').join(''):'<p class="empty">无</p>')+'</div></div></section>';
}

export function bindKitchen(refresh,stall){
 if(stall)sessionStorage.setItem('kitchen-stall',JSON.stringify(stall));
 const goToStall=name=>{
   sessionStorage.setItem('kitchen-stall',JSON.stringify(name));
   if(location.hash==='#/kitchen')refresh();else location.hash='#/kitchen';
 };
 document.querySelector('#refresh-kitchen')?.addEventListener('click',refresh);
 document.querySelectorAll('[data-stall]').forEach(b=>b.onclick=()=>goToStall(b.dataset.stall));
 const stallForm=document.querySelector('#stall-form');
 if(stallForm)submit(stallForm,async f=>{const name=String(f.get('stall')).trim();if(!name)throw new Error('档口名称不能为空');goToStall(name);});
 const adoptForm=document.querySelector('#adoption-form');
 if(adoptForm)submit(adoptForm,async f=>{
   const job_id=Number(f.get('job_id'));
   if(!job_id)throw new Error('请先选择已完成且获批准的分析');
   const qty=f.get('qty');
   const key=adoptForm.dataset.opKey||newOperationId();
   adoptForm.dataset.opKey=key;remember(key);
   const res=await kitchen.adopt(key,stall,{job_id,days:[{day:f.get('day'),qty:qty===''?undefined:Number(qty),reason:f.get('reason'),note:f.get('note')}]});
   delete adoptForm.dataset.opKey;
   notify(res.duplicate?'该操作此前已执行，本次为重试，未重复处理':'已登记采用建议，受影响区间已重算');
   await refresh();
 });
 const batchForm=document.querySelector('#batch-form');
 if(batchForm)submit(batchForm,async f=>{
   const key=batchForm.dataset.opKey||newOperationId();
   batchForm.dataset.opKey=key;remember(key);
   const res=await kitchen.batches(key,stall,{source:'manual.json',batches:[{day:f.get('day'),batch_ref:f.get('batch_ref'),qty:Number(f.get('qty')),note:f.get('note')}]});
   delete batchForm.dataset.opKey;
   notify(res.duplicate?'该操作此前已执行，本次为重试，未重复处理':'已登记实领批次，受影响区间已重算');
   await refresh();
 });
 document.querySelectorAll('[data-quick-adopt]').forEach(b=>b.onclick=()=>{const form=document.querySelector('#adoption-form');form.querySelector('[name=day]').value=b.dataset.quickAdopt;form.scrollIntoView({behavior:'smooth',block:'center'});form.querySelector('[name=qty]').focus();});
 document.querySelectorAll('[data-confirm]').forEach(b=>b.onclick=async()=>{
   try{const a=await kitchen.alert(b.dataset.confirm);
     await kitchen.confirm(a.id,{operation_id:newOperationId(),stall:a.stall,basis_checksum:a.basis_checksum,note:'页面确认'});
     notify('已确认处理，原依据已留存');await refresh();
   }catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
   const reason=prompt('撤销原因（仅未处理预警可撤销）');if(!reason)return;
   try{const a=await kitchen.alert(b.dataset.revoke);await kitchen.revoke(a.id,{operation_id:newOperationId(),stall:a.stall,reason});notify('未处理预警已撤销');await refresh();}catch(e){notify(e.message,true);}
 });
 const recover=document.querySelector('#recover-form');
 if(recover)submit(recover,async f=>{const key=String(f.get('operation_id')).trim();const res=await kitchen.recover(key);notify(res.duplicate?'该操作此前已完成，本次未重复处理':'已按操作编号重放，未重复开预警或追加说明');await refresh();});
 const confirmForm=document.querySelector('#confirm-form');
 if(confirmForm)submit(confirmForm,async f=>{
   const a=await kitchen.alert(Number(location.hash.split('/').pop()));
   await kitchen.confirm(a.id,{operation_id:newOperationId(),stall:a.stall,basis_checksum:a.basis_checksum,note:f.get('note')});
   notify('已确认处理，原依据已留存');await refresh();
 });
 const revokeForm=document.querySelector('#revoke-form');
 if(revokeForm)submit(revokeForm,async f=>{
   const a=await kitchen.alert(Number(location.hash.split('/').pop()));
   await kitchen.revoke(a.id,{operation_id:newOperationId(),stall:a.stall,reason:f.get('reason')});
   notify('未处理预警已撤销');await refresh();
 });
}

