"use strict";
const $ = id => document.getElementById(id);
const state = {guild:"", token:"", page:1, pages:1, selected:null, review:null, listSeq:0, detailSeq:0, adminSeq:0, section:"reports", reviewSeq:0, jobs:new Set()};
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined) el.textContent=String(text); if(cls) el.className=cls; return el; }
function notice(text="") { $("notice-text").textContent=text; $("notice").hidden=!text; }
function badge(status) { return node("span", status, "badge" + (["Resolved","Not Resolved"].includes(status)?" closed":status==="Claimed"?" claimed":"")); }
function username(user) { return user ? `${user.username} (${user.id})` : "Unassigned"; }
function date(value) { if(!value) return "—"; const d=new Date(value); return Number.isNaN(d.getTime())?String(value):d.toLocaleString(); }
function link(label, url) { if(!url) return null; try { const parsed=new URL(url); if(!["http:","https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null; const a=node("a",label); a.href=parsed.href; a.target="_blank"; a.rel="noopener noreferrer"; return a; } catch { return null; } }
function option(select, value, label) { const o=node("option",label); o.value=value; select.append(o); }
async function api(path, data) {
  const response=await fetch(path, {method:data?"POST":"GET",credentials:"same-origin",headers:data?{"Content-Type":"application/json","X-Session-Token":state.token}:{},body:data?JSON.stringify(data):undefined});
  const value=await response.json(); if(!response.ok) throw new Error(value.error || "The request failed. Refresh and check the current state."); return value;
}
function params(extra={}) { return new URLSearchParams({guild_id:state.guild,...extra}); }
function attempt(work) { return async event => { event?.preventDefault(); try { await work(event); } catch(error) { notice(error.message); } }; }
function fields(el, values) { const dl=node("dl"); for(const [label,value] of values) { dl.append(node("dt",label),node("dd", value ?? "—")); } el.append(dl); }
async function loadReports() {
  const seq=++state.listSeq, guild=state.guild;
  const data=await api("/api/reports?"+params({page:state.page,q:$("search").value,status:$("status").value,type:$("type").value,assigned:$("assigned").value,provider:$("provider").value}));
  if(seq!==state.listSeq || guild!==state.guild) return;
  for(const key of ["active","claimed","tickets","closed"]) $("count-"+key).textContent=data.counts[key];
  const previous=$("provider").value; $("provider").replaceChildren(); option($("provider"),"","All providers"); for(const p of data.providers) option($("provider"),p,p); $("provider").value=previous;
  state.pages=data.pages; $("report-total").textContent=`${data.total} matching`; $("page-label").textContent=`${data.page} / ${data.pages}`; $("prev").disabled=state.page<=1; $("next").disabled=state.page>=data.pages;
  $("report-list").replaceChildren();
  if(!data.items.length) $("report-list").append(node("div","No reports match these filters.","empty muted"));
  for(const report of data.items) {
    const button=node("button",undefined,"report-row"+(state.selected===report.id?" selected":"")); button.type="button"; button.dataset.reportId=report.id; button.setAttribute("aria-pressed",String(state.selected===report.id));
    const top=node("div",undefined,"row-top"); top.append(node("strong",`#${report.id} · ${report.subject}`),badge(report.closing?"Closure pending":report.status));
    button.append(top,node("span",`${report.report_type==="TV"?"Live TV":"VOD"} · ${username(report.reporter)} · ${date(report.created_at)}`,"row-meta"),node("span",String(report.payload.issue||"No issue description").slice(0,180),"row-issue"));
    button.addEventListener("click",attempt(()=>loadReport(report.id))); $("report-list").append(button);
  }
}
async function loadReport(id=state.selected) {
  if(!id) return; const seq=++state.detailSeq, guild=state.guild;
  const r=await api("/api/report?"+params({report_id:id})); if(seq!==state.detailSeq || guild!==state.guild) return;
  state.selected=r.id;
  for(const row of document.querySelectorAll("[data-report-id]")) { const chosen=row.dataset.reportId===r.id; row.classList.toggle("selected",chosen); row.setAttribute("aria-pressed",String(chosen)); }
  const el=$("detail"); el.replaceChildren(); el.append(node("p",`REPORT #${r.id} · ${r.report_type==="TV"?"LIVE TV":"VOD"}`,"eyebrow"),node("h2",r.subject),badge(r.closing?"Closure pending":r.status));
  const links=node("div",undefined,"links"); for(const item of [link("Open staff message ↗",r.staff_url),link("Open ticket ↗",r.ticket_url),link("Title reference ↗",r.payload.reference_link)]) if(item) links.append(item); el.append(links);
  fields(el,[["Reporter",username(r.reporter)],["Claimed by",username(r.claimant)],["Created",date(r.created_at)],["Last updated",date(r.updated_at)],["Claimed",date(r.claimed_at)],["Closed",date(r.resolved_at)],["Closed by",r.resolver?username(r.resolver):"—"]]);
  el.append(node("h2","Issue description"),node("div",r.payload.issue||"Not provided","issue"));
  const names={channel_name:"Channel",provider_name:"Provider",provider_id:"Provider ID",title:"Title",english_title:"English title",device:"Device",content_type:"Content type",language:"Library",is_4k:"4K",is_remux:"Remux",requested_via_bot:"Requested via bot",season_number:"Season",episode_number:"Episode",episode_scope:"Episode scope",episode_title:"Episode title",reference_link:"Reference"};
  fields(el,Object.entries(r.payload).filter(([key])=>key!=="issue").map(([key,value])=>[names[key]||key.replaceAll("_"," "),typeof value==="object"?JSON.stringify(value):value]));
  if(r.resolved_note) el.append(node("h2","Staff closure details"),node("div",r.resolved_note,"issue"));
  if(r.closing) { el.append(node("p","A closure is in progress. Anyone in the ticket can use Cancel or send a message to keep the ticket and report open.","notice")); return; }
  const closed=["Resolved","Not Resolved"].includes(r.status);
  const form=node("form",undefined,"stack"); const noteLabel=node("label","Reporter update / closure details"); const note=node("textarea"); note.maxLength=1000; noteLabel.append(note); form.append(noteLabel);
  const userLabel=node("label","New reporter's user ID (for Change reporter)"); const user=node("input"); user.inputMode="numeric"; userLabel.append(user); form.append(userLabel);
  const actions=node("div",undefined,"actions"); const choices=closed?[["reopen","Reopen"]]:[["claim","Claim"],["ticket","Open ticket"],["resolve","Resolved"],["not_resolved","Not resolved"]];
  if(!r.staff_message_id && !closed) choices.push(["resend","Send missing staff report"]); choices.push(["reassign","Change reporter"]);
  for(const [action,label] of choices) { const b=node("button",label); b.type="button"; b.disabled=(action==="claim"&&(r.status!=="Open"||!!r.claimed_by_user_id||!!r.ticket_channel_id||!r.staff_message_id))||(action==="ticket"&&(!!r.ticket_channel_id||!r.staff_message_id))||(action==="reopen"&&!r.staff_message_id)||(action==="reassign"&&!!r.ticket_channel_id); b.addEventListener("click",attempt(()=>review({action,report_id:r.id,note:note.value,user_id:user.value}))); actions.append(b); }
  form.append(actions); form.addEventListener("submit",e=>e.preventDefault()); el.append(form);
}
function showEmbed(embed, parent) { const box=node("div",undefined,"preview-embed"); if(embed.author?.name) box.append(node("strong",embed.author.name)); if(embed.title) box.append(node("h2",embed.title)); if(embed.description) box.append(node("p",embed.description)); for(const field of embed.fields||[]) { box.append(node("strong",field.name),node("p",field.value)); } for(const prop of ["image","thumbnail"]) if(embed[prop]?.url) box.append(node("p",`${prop}: ${embed[prop].url}`,"muted")); if(embed.footer?.text) box.append(node("p",embed.footer.text,"muted")); parent.append(box); }
async function review(data) {
  const guild=state.guild, seq=++state.reviewSeq;
  const response=await api("/api/preview",{...data,guild_id:guild});
  if(guild!==state.guild || seq!==state.reviewSeq) { await api("/api/discard",{review_id:response.review_id}).catch(()=>{}); return; }
  state.review=response.review_id; $("review-error").hidden=true; $("review-error").textContent="";
  const p=response.preview; $("review-title").textContent=p.title; const content=$("review-content"); content.replaceChildren();
  if(p.subject) fields(content,[["Report",`#${p.report_id} · ${p.subject}`],["Status",p.status],["Reporter",p.reporter_id]]);
  content.append(node("p",`Server: ${p.server}`)); for(const detail of p.details) content.append(node("p",detail));
  if(p.content) content.append(node("div",p.content,"issue"));
  if(p.embed) showEmbed(p.embed,content);
  for(const r of p.reports||[]) content.append(node("p",`#${r.id} · ${r.subject}${r.ticket?` · ticket ${r.ticket}`:""}`));
  $("confirmation").value=""; $("review-confirm").disabled=false; $("review-cancel").disabled=false; $("review-dialog").showModal(); $("confirmation").focus();
}
async function cancelReview() { const rid=state.review; state.reviewSeq++; try { if(rid) await api("/api/discard",{review_id:rid}); } finally { state.review=null; $("review-dialog").close(); } }
async function poll(rid,guild) {
  if(guild!==state.guild) return;
  const data=await api("/api/review?"+new URLSearchParams({review_id:rid}));
  if(guild!==state.guild) return;
  if(data.state==="running") { window.setTimeout(()=>poll(rid,guild).catch(e=>notice(e.message)),1500); return; }
  state.jobs.delete(rid);
  const messages=[...(data.messages||[])]; for(const r of data.reports||[]) messages.push(...r.messages.map(m=>`#${r.report_id}: ${m}`));
  notice(data.error||messages.join("\n")||"Action finished. Check the current report state."); await refresh();
}
async function confirm(event) {
  event.preventDefault(); if(!state.review) return; const rid=state.review, guild=state.guild;
  $("review-confirm").disabled=true; $("review-cancel").disabled=true;
  try { const result=await api("/api/confirm",{review_id:rid,confirmation:$("confirmation").value}); state.review=null; $("review-dialog").close(); notice("Action started. Ticket closures keep the cancellation grace period; follow progress in the ticket."); state.jobs.add(rid); await poll(rid,guild); if(result.state==="running") await refresh(); }
  catch(error) { $("review-error").textContent=error.message; $("review-error").hidden=false; }
  finally { $("review-confirm").disabled=false; $("review-cancel").disabled=false; }
}
async function loadAdmin() {
  const seq=++state.adminSeq,guild=state.guild; const data=await api("/api/admin?"+params()); if(seq!==state.adminSeq||guild!==state.guild) return;
  const previous=$("channel").value; $("channel").replaceChildren(); for(const ch of data.channels) option($("channel"),ch.id,`${ch.name} (${ch.id})`); if(data.channels.some(c=>c.id===previous)) $("channel").value=previous;
  $("board-status").textContent=data.liveboard?`Liveboard configured · message ${data.liveboard.message_id}`:"No liveboard configured."; $("board-refresh").disabled=$("board-stop").disabled=!data.liveboard;
  $("owner-controls").hidden=!data.owner; $("pings-state").textContent=`New-report staff pings: ${data.pings?"On":"Off"}`;
  $("blocks").replaceChildren(node("h2","Saved report blocks","spaced")); if(!data.blocks.length) $("blocks").append(node("p","No saved report blocks.","muted"));
  for(const b of data.blocks) { const card=node("div",undefined,"block"); card.append(node("strong",username(b.user)),node("p",b.is_permanent?"Permanent":`Until ${date(b.expires_at)}`,"muted"),node("p",b.reason||"No reason saved.")); const button=node("button","Review unblock"); button.addEventListener("click",attempt(()=>review({action:"unblock",user_id:b.user_id}))); card.append(button); $("blocks").append(card); }
  $("settings").replaceChildren(); for(const [k,v] of Object.entries(data.settings)) $("settings").append(node("dt",k),node("dd",v));
}
async function refresh() { if(state.section==="admin") await loadAdmin(); else { await loadReports(); await loadReport(); } }
async function navigate(section) { state.section=section; $("reports").hidden=section!=="reports"; $("admin").hidden=section!=="admin"; for(const id of ["reports","admin"]) { $("nav-"+id).classList.toggle("active",id===section); $("nav-"+id).setAttribute("aria-pressed",String(id===section)); } await refresh(); }
async function start() {
  const data=await api("/api/bootstrap"); state.token=data.token; if(data.preview) notice("Fictional preview · changes and Discord traffic are disabled."); $("signed-in").textContent=`Staff · ${data.user_id}`; for(const guild of data.guilds) option($("guild"),guild.id,guild.name); state.guild=$("guild").value; await loadReports();
}
$("dismiss").addEventListener("click",()=>notice());
$("filters").addEventListener("submit",attempt(async()=>{state.page=1;await loadReports();}));
for(const filter of ["status","type","assigned","provider"]) $(filter).addEventListener("change",attempt(async()=>{state.page=1;await loadReports();}));
$("prev").addEventListener("click",attempt(async()=>{if(state.page>1){state.page--;await loadReports();}})); $("next").addEventListener("click",attempt(async()=>{if(state.page<state.pages){state.page++;await loadReports();}}));
$("refresh").addEventListener("click",attempt(refresh)); $("admin-refresh").addEventListener("click",attempt(loadAdmin));
$("nav-reports").addEventListener("click",attempt(()=>navigate("reports"))); $("nav-admin").addEventListener("click",attempt(()=>navigate("admin")));
$("guild").addEventListener("change",attempt(async()=>{if(state.review) await cancelReview(); state.guild=$("guild").value; state.reviewSeq++; state.page=1; state.selected=null; state.detailSeq++; $("provider").value=""; $("detail").replaceChildren(node("div","Select a report.","empty muted")); await refresh();}));
$("logout").addEventListener("click",attempt(async()=>{await api("/auth/logout",{}); window.location.href="/";}));
$("review-form").addEventListener("submit",attempt(confirm)); $("review-cancel").addEventListener("click",attempt(cancelReview)); $("review-dialog").addEventListener("cancel",attempt(cancelReview));
$("block-form").addEventListener("submit",attempt(()=>review({action:"block",user_id:$("block-user").value,duration_minutes:$("block-duration").value,note:$("block-reason").value})));
for(const [id,action] of [["panel-action","panel"],["board-start","liveboard_start"],["board-refresh","liveboard_refresh"],["board-stop","liveboard_stop"],["bulk-close","bulk_close"],["pings-action","pings"],["sync-action","sync"]]) $(id).addEventListener("click",attempt(()=>review({action,channel_id:$("channel").value})));
start().catch(error=>notice(error.message));
