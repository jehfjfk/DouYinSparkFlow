const state={config:null,status:null,view:"overview",logTimer:null,statusTimer:null};
const titles={overview:["运行概览","检查账号状态并启动今日任务"],accounts:["账号与好友","管理登录凭证和发送范围"],message:["消息设置","编辑每天发送的消息内容"],runtime:["运行控制","调整参数并管理任务进程"],logs:["运行日志","查看任务执行详情和异常"]};
const hitokotoOptions=["动画","漫画","游戏","文学","原创","来自网络","影视","诗词","哲学","抖机灵","其他"];
const $=selector=>document.querySelector(selector);
const $$=selector=>Array.from(document.querySelectorAll(selector));

async function api(path,options={}){
  const response=await fetch(path,Object.assign({headers:{"Content-Type":"application/json"}},options));
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||"请求失败");
  return data;
}
function toast(message,error=false){
  const node=document.createElement("div");
  node.className="toast"+(error?" error":"");
  node.textContent=message;
  $("#toastRegion").append(node);
  setTimeout(()=>node.remove(),3600);
}
function escapeHtml(value){
  return String(value).replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}
function switchView(view){
  state.view=view;
  $$("[data-view-panel]").forEach(el=>el.classList.toggle("active",el.dataset.viewPanel===view));
  $$(".nav-item").forEach(el=>el.classList.toggle("active",el.dataset.view===view));
  $("#pageTitle").textContent=titles[view][0];
  $("#pageSubtitle").textContent=titles[view][1];
  $("#sidebar").classList.remove("open");
  if(view==="logs")loadLogs();
}
function accountMarkup(account,index){
  const targets=account.targets.map((target,i)=>'<span class="editable-chip"><strong>'+escapeHtml(target.id)+'</strong>'+(target.aliases.length?' · '+escapeHtml(target.aliases.join(" / ")):'')+'<button data-action="remove-target" data-target-index="'+i+'" title="移除">×</button></span>').join("");
  return '<article class="account-card" data-index="'+index+'">'+
    '<div class="account-head"><span class="account-index">账号 '+String(index+1).padStart(2,"0")+'</span><div><span class="account-status">'+
    (account.cookieConfigured?"● Cookie 已配置 · "+account.cookieCount+" 条":"○ Cookie 未配置")+'</span><button class="text-button" data-action="login-refresh" title="网页登录并自动更新 Cookie">更新登录</button><button class="text-button" data-action="run-account" title="仅运行此账号">运行此账号</button><button class="text-button remove-account" data-action="remove-account" title="删除账号">删除</button></div></div>'+
    '<div class="account-body">'+
    '<label class="field"><span>用户名</span><input data-field="username" value="'+escapeHtml(account.username)+'"></label>'+
    '<label class="field"><span>抖音号</span><input data-field="uniqueId" value="'+escapeHtml(account.uniqueId)+'"></label>'+
    '<div class="field full"><span>目标好友</span><div class="target-editor target-fields"><input data-role="target-id" placeholder="好友抖音号"><input data-role="target-aliases" placeholder="昵称或备注，多个用逗号分隔"><button class="button secondary" data-action="add-target">添加</button></div><div class="chips-editor">'+targets+'</div></div>'+
    '<div class="field full"><span>Cookie JSON</span><div class="cookie-row"><textarea data-field="cookies" placeholder="已有 Cookie 不会回显。仅在需要更新时粘贴新的 JSON。"></textarea><div class="cookie-badge"><strong>'+(account.cookieCount||0)+'</strong><span>'+(account.cookieConfigured?"已安全保存":"等待导入")+'</span></div></div></div>'+
    '</div></article>';
}
function renderAccounts(){
  $("#accountList").innerHTML=state.config.accounts.map(accountMarkup).join("");
  bindAccountEvents();
  renderOverview();
}
function bindAccountEvents(){
  $$(".account-card").forEach(card=>{
    const index=Number(card.dataset.index);
    card.querySelectorAll("[data-field]").forEach(input=>input.addEventListener("input",()=>{
      if(input.dataset.field!=="cookies")state.config.accounts[index][input.dataset.field]=input.value;
    }));
    const add=()=>{
      const id=card.querySelector('[data-role="target-id"]').value.trim();
      const aliases=card.querySelector('[data-role="target-aliases"]').value.split(/[，,]/).map(value=>value.trim()).filter(Boolean);
      if(id&&!state.config.accounts[index].targets.some(target=>target.id===id)){state.config.accounts[index].targets.push({id,aliases});renderAccounts();}
    };
    card.querySelector('[data-action="add-target"]').addEventListener("click",add);
    card.querySelectorAll('[data-role^="target-"]').forEach(input=>input.addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();add();}}));
    card.querySelector('[data-action="remove-account"]').addEventListener("click",()=>{
      if(confirm("删除此账号及好友配置？同步后对应 Cookie Secret 也会删除。")){state.config.accounts.splice(index,1);renderAccounts();}
    });
    card.querySelector('[data-action="run-account"]').addEventListener("click",()=>runSingleAccount(index));
    card.querySelector('[data-action="login-refresh"]').addEventListener("click",()=>refreshAccountLogin(index));
    card.querySelectorAll('[data-action="remove-target"]').forEach(button=>button.addEventListener("click",()=>{
      state.config.accounts[index].targets.splice(Number(button.dataset.targetIndex),1);renderAccounts();
    }));
  });
}
function renderMessage(){
  $("#messageTemplate").value=state.config.messageTemplate;
  $("#messageCounter").textContent=state.config.messageTemplate.length+" 字";
  $("#messagePreview").textContent=state.config.messageTemplate.replace("[API]","这里是每日一言");
  $("#hitokotoTypes").innerHTML=hitokotoOptions.map(type=>'<label class="check-item"><input type="checkbox" value="'+type+'" '+(state.config.hitokotoTypes.includes(type)?"checked":"")+">"+type+"</label>").join("");
  $$("#hitokotoTypes input").forEach(input=>input.addEventListener("change",()=>{state.config.hitokotoTypes=$$("#hitokotoTypes input:checked").map(el=>el.value);}));
}
function renderRuntimeForm(){
  ["scheduleTime","matchMode","logLevel","browserTimeout","friendListWaitTime","taskRetryTimes"].forEach(id=>$("#"+id).value=state.config[id]);
}
function renderOverview(){
  if(!state.config)return;
  const accounts=state.config.accounts;
  const targetCount=accounts.reduce((sum,a)=>sum+a.targets.length,0);
  const ready=accounts.filter(a=>a.cookieConfigured).length;
  $("#metricAccounts").textContent=accounts.length;
  $("#metricTargets").textContent=targetCount;
  $("#metricCookies").textContent=ready+"/"+accounts.length;
  $("#cookieHint").textContent=ready===accounts.length?"全部账号可用":"存在未配置账号";
  $("#overviewMessage").textContent=state.config.messageTemplate;
  const targets=accounts.flatMap(a=>a.targets);
  $("#overviewTargets").innerHTML=targets.slice(0,8).map(item=>'<span class="target-chip">'+escapeHtml(item.id)+'</span>').join("")+(targets.length>8?'<span class="target-chip more">+'+(targets.length-8)+"</span>":"");
}
function renderStatus(){
  if(!state.status)return;
  const running=state.status.running;
  $("#statusPill").classList.toggle("running",running);
  $("#statusPill span").textContent=running?"任务运行中":"系统待机";
  $("#metricRuntime").textContent=running?"运行中":"待机";
  $("#runtimeHint").textContent=running?"PID "+state.status.pid:(state.status.lastExitCode===null?"尚未启动":"退出码 "+state.status.lastExitCode);
  $("#runtimeTitle").textContent=running?"任务运行中":"任务待机";
  $("#runtimeDescription").textContent=running?"浏览器正在处理好友列表":"配置保存后即可开始发送";
  $("#runtimeIcon").textContent=running?"••":"▶";
  $("#runtimePid").textContent=state.status.pid||"--";
  $("#runtimeStarted").textContent=state.status.startedAt?new Date(state.status.startedAt).toLocaleString():"--";
  $("#runtimeExit").textContent=state.status.lastExitCode===null?"--":state.status.lastExitCode;
  ["overviewRun","runtimeRun"].forEach(id=>$("#"+id).classList.toggle("hidden",running));
  ["overviewStop","runtimeStop"].forEach(id=>$("#"+id).classList.toggle("hidden",!running));
}
function collectConfig(){
  const config=Object.assign({},state.config);
  config.accounts=config.accounts.map((account,index)=>{
    const card=document.querySelector('.account-card[data-index="'+index+'"]');
    return Object.assign({},account,{
      username:(card&&card.querySelector('[data-field="username"]').value.trim())||account.username,
      uniqueId:(card&&card.querySelector('[data-field="uniqueId"]').value.trim())||account.uniqueId,
      cookies:(card&&card.querySelector('[data-field="cookies"]').value.trim())||""
    });
  });
  ["scheduleTime","matchMode","logLevel"].forEach(id=>config[id]=$("#"+id).value);
  ["browserTimeout","friendListWaitTime","taskRetryTimes"].forEach(id=>config[id]=Number($("#"+id).value));
  config.messageTemplate=$("#messageTemplate").value;
  return config;
}
async function saveConfig(){
  try{
    const result=await api("/api/config",{method:"POST",body:JSON.stringify(collectConfig())});
    state.config=result.config;clearScanResults();renderAll();toast("配置已保存");
  }catch(error){toast(error.message,true);throw error;}
}
async function syncGithub(){
  const button=$("#syncButton");
  try{
    button.disabled=true;button.textContent="正在同步...";
    await saveConfig();
    const result=await api("/api/github/sync",{method:"POST",body:"{}"});
    toast("已同步 "+result.result.accounts+" 个账号到 GitHub");
  }catch(error){toast(error.message,true);}finally{button.disabled=false;button.textContent="同步到 GitHub";}
}
async function scanPinned(){
  if(!state.config.accounts.length){toast("请先添加并保存一个账号",true);return;}
  const index=prompt("扫描哪个账号？请输入序号（1-"+state.config.accounts.length+"）：", "1");
  if(index===null)return;
  let progressTimer=null;
  try{
    $("#scanProgress").classList.remove("hidden");
    setScanProgress(0,"准备扫描");
    progressTimer=setInterval(loadScanProgress,400);
    toast("正在打开创作者中心并扫描置顶会话...");
    const result=await api("/api/scan-pinned",{method:"POST",body:JSON.stringify({accountIndex:Number(index)-1})});
    state.scan=result.result;renderScanResults();toast(result.result.message||"扫描完成，请确认后加入配置");
  }catch(error){toast(error.message,true);}finally{if(progressTimer)clearInterval(progressTimer);await loadScanProgress();setTimeout(()=>$("#scanProgress").classList.add("hidden"),1400);}
}
function setScanProgress(percent,stage){
  $("#scanProgressFill").style.width=percent+"%";
  $("#scanPercent").textContent=percent+"%";
  $("#scanStage").textContent=stage;
}
async function loadScanProgress(){try{const status=await api("/api/scan-status");setScanProgress(status.percent,status.stage);const row=$("#loginLinkRow");if(status.qrImage){$("#loginQr").src=status.qrImage;row.classList.remove("hidden");}else{row.classList.add("hidden");}}catch(_error){}}
function renderScanResults(){
  $("#scanPanel").classList.remove("hidden");
  $("#scanCaption").textContent="扫描来源：账号 "+(state.scan.accountIndex+1)+"（"+state.scan.account+"）。勾选后加入此账号。";
  $("#scanResults").innerHTML=(state.scan.contacts||[]).map((item,i)=>'<label class="scan-row"><input type="checkbox" data-scan-index="'+i+'" checked><span><strong>'+escapeHtml(item.uniqueId||item.shortId||"未获取抖音号")+'</strong><small>'+escapeHtml(item.nickname)+(item.remark&&item.remark!==item.nickname?' · '+escapeHtml(item.remark):'')+'</small></span></label>').join("")||'<div class="empty">未识别到置顶会话。普通消息联系人不会加入扫描结果。</div>';
}
function clearScanResults(){
  state.scan=null;
  $("#scanPanel").classList.add("hidden");
  $("#scanResults").innerHTML="";
}
function importScanned(){
  if(!state.scan)return;
  const index=state.scan.accountIndex;
  if(index<0||!state.config.accounts[index])return;
  const selected=$$("[data-scan-index]:checked").map(input=>state.scan.contacts[Number(input.dataset.scanIndex)]).filter(item=>item.uniqueId||item.shortId);
  selected.forEach(item=>{const id=item.uniqueId||item.shortId;const aliases=[item.nickname,item.remark].filter(Boolean);if(!state.config.accounts[index].targets.some(target=>target.id===id))state.config.accounts[index].targets.push({id,aliases});});
  renderAccounts();clearScanResults();toast("已加入账号 "+(index+1)+" 的 "+selected.length+" 个好友，请保存配置");
}
async function requestRun(){try{await saveConfig();$("#confirmModal").classList.remove("hidden");}catch(_error){}}
async function startRun(){
  try{const result=await api("/api/run",{method:"POST",body:"{}"});state.status=result.status;$("#confirmModal").classList.add("hidden");renderStatus();toast("任务已启动");}
  catch(error){toast(error.message,true);}
}
async function runSingleAccount(index){
  try{
    await saveConfig();
    const account=state.config.accounts[index];
    if(!account)return;
    if(!confirm("仅运行账号“"+account.username+"”，向该账号配置的 "+account.targets.length+" 位好友真实发送消息？"))return;
    const result=await api("/api/run-account",{method:"POST",body:JSON.stringify({accountId:account.uniqueId})});
    state.status=result.status;renderStatus();toast("已启动账号 "+account.username);
  }catch(error){toast(error.message,true);}
}
async function refreshAccountLogin(index){
  const account=state.config.accounts[index];
  if(!account||!account.uniqueId){toast("请先填写并保存账号抖音号",true);return;}
  let timer=null;
  try{
    $("#scanProgress").classList.remove("hidden");setScanProgress(0,"准备打开登录窗口");
    timer=setInterval(loadScanProgress,500);
    let result=null,requestError=null;
    try{result=await api("/api/account-login-refresh",{method:"POST",body:JSON.stringify({accountId:account.uniqueId})});}catch(error){requestError=error;}
    let loginStatus=await api("/api/scan-status");
    const deadline=Date.now()+6*60*1000;
    while(loginStatus.running&&Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,1500));loginStatus=await api("/api/scan-status");}
    if(loginStatus.error)throw new Error(loginStatus.error);
    if(!loginStatus.stage.includes("登录已更新")){throw requestError||new Error("登录更新未完成");}
    account.cookieConfigured=true;if(result)account.cookieCount=result.result.cookieCount;renderAccounts();toast("登录状态已更新，正在扫描置顶好友");
    setScanProgress(0,"登录已更新，开始扫描置顶好友");
    const scan=await api("/api/scan-pinned",{method:"POST",body:JSON.stringify({accountIndex:index})});
    state.scan=scan.result;renderScanResults();toast(scan.result.message||"置顶好友扫描完成");
  }catch(error){toast(error.message,true);}finally{if(timer)clearInterval(timer);await loadScanProgress();setTimeout(()=>$("#scanProgress").classList.add("hidden"),1800);}
}
async function stopRun(){
  try{const result=await api("/api/stop",{method:"POST",body:"{}"});state.status=result.status;renderStatus();toast("任务已停止");}
  catch(error){toast(error.message,true);}
}
async function loadStatus(){try{state.status=await api("/api/status");renderStatus();}catch(_error){}}
async function loadLogs(){
  try{
    const data=await api("/api/logs?limit=300");
    const consoleEl=$("#logConsole");
    consoleEl.innerHTML=data.entries.length?data.entries.map(entry=>'<div class="log-line"><span class="log-source">'+entry.source+'</span><span>'+escapeHtml(entry.text)+"</span></div>").join(""):'<div class="empty">暂无日志</div>';
    if($("#autoScroll").checked)consoleEl.scrollTop=consoleEl.scrollHeight;
    const recent=data.entries.slice(-6).reverse();
    $("#recentActivity").innerHTML=recent.length?recent.map(entry=>'<div class="activity-item"><i></i><div><strong>'+escapeHtml(entry.text)+"</strong><small>"+entry.source+"</small></div></div>").join(""):'<div class="empty">暂无日志</div>';
  }catch(_error){}
}
function renderAll(){renderAccounts();renderMessage();renderRuntimeForm();renderOverview();renderStatus();}
function bindStaticEvents(){
  $$(".nav-item").forEach(button=>button.addEventListener("click",()=>switchView(button.dataset.view)));
  $$("[data-jump]").forEach(button=>button.addEventListener("click",()=>switchView(button.dataset.jump)));
  $("#menuButton").addEventListener("click",()=>$("#sidebar").classList.toggle("open"));
  $("#saveButton").addEventListener("click",saveConfig);
  $("#syncButton").addEventListener("click",syncGithub);
  $("#openCreator").addEventListener("click",()=>window.open("https://creator.douyin.com/", "_blank", "noopener"));
  $("#scanPinned").addEventListener("click",scanPinned);
  $("#importScanned").addEventListener("click",importScanned);
  $("#messageTemplate").addEventListener("input",event=>{
    state.config.messageTemplate=event.target.value;
    $("#messageCounter").textContent=event.target.value.length+" 字";
    $("#messagePreview").textContent=event.target.value.replace("[API]","这里是每日一言");
    renderOverview();
  });
  ["overviewRun","runtimeRun"].forEach(id=>$("#"+id).addEventListener("click",requestRun));
  ["overviewStop","runtimeStop"].forEach(id=>$("#"+id).addEventListener("click",stopRun));
  $("#cancelRun").addEventListener("click",()=>$("#confirmModal").classList.add("hidden"));
  $("#confirmRun").addEventListener("click",startRun);
  $("#refreshLogs").addEventListener("click",loadLogs);
  $("#addAccount").addEventListener("click",()=>{clearScanResults();state.config.accounts.push({username:"",uniqueId:"",targets:[],cookieConfigured:false,cookieCount:0});renderAccounts();switchView("accounts");});
}
async function init(){
  bindStaticEvents();
  $("#todayDate").textContent=new Date().toLocaleDateString("zh-CN",{month:"long",day:"numeric",weekday:"short"});
  try{
    const results=await Promise.all([api("/api/config"),api("/api/status")]);
    state.config=results[0];state.status=results[1];renderAll();await loadLogs();
    state.statusTimer=setInterval(loadStatus,3000);
    state.logTimer=setInterval(()=>{if(state.view==="logs"||state.view==="overview")loadLogs();},5000);
  }catch(error){toast(error.message,true);}
}
init();
