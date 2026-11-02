/* 数据库页：所有查询针对本地快照，筛选参数不拼成任意 SQL。 */
(()=>{
  const root = document.getElementById('carddb');
  if (!root) return;
  const el = id => document.getElementById(id);
  const names = ['cloudsync.db','diagnose_db.db','mqtt.db','tasks_db.db',
    'tasks_monitor_db.db','tasks_record_db.db','maps.db'];
  const snapshots = new Map();
  let page = 0, busy = false, generation = 0, current = null, batch = [];
  let applied = {field:'',value:'',sort:'',desc:false};
  const status = text => { el('dbstatus').textContent = text; };
  // 使用 textContent 创建单元格，数据库内容不能被当成页面 HTML 执行。
  function node(tag, text){ const n=document.createElement(tag); n.textContent=text; return n; }
  function options(id, values, placeholder){
    const select=el(id); select.replaceChildren();
    if (placeholder) { const option=node('option',placeholder); option.value=''; select.append(option); }
    values.forEach(value=>{const option=node('option',value);option.value=value;select.append(option);});
  }
  names.forEach(name=>{
    const label=node('label',''), check=document.createElement('input');
    check.type='checkbox'; check.value=name; check.style.width='auto';
    const info=node('span',' '+name);info.dataset.database=name;
    label.append(check,info); el('dbsources').append(label);
  });
  async function request(body, binary=false){
    const response=await fetch('/api/database',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (!response.ok) {const j=await response.json();throw Error(j.err||'操作失败');}
    return binary ? response.blob() : response.json();
  }
  function download(blob, filename){
    const url=URL.createObjectURL(blob), anchor=document.createElement('a');
    anchor.href=url; anchor.download=filename; anchor.click();
    setTimeout(()=>URL.revokeObjectURL(url),30000); // 给浏览器时间读取下载数据。
  }
  function enable(){
    const table=!!el('dbtable').value;
    el('dbcapture').disabled=busy;
    el('dblist').disabled=busy;el('dbzip').disabled=busy||!batch.length;
    ['dbsnapshot','dbtable','dbfield','dbvalue','dbstart','dbend','dbsort','dbdirection'].forEach(id=>{el(id).disabled=busy;});
    el('dbapply').disabled=busy||!table;
    el('dbcsv').disabled=busy||!table;
    el('dbdownload').disabled=busy||!el('dbsnapshot').value;
  }
  function clearData(){
    el('dbdata').replaceChildren(); el('dbschema').textContent='';
    el('dbprev').disabled=true; el('dbnext').disabled=true;
    el('dbpage').textContent='每页 100 行'; current=null;
  }
  async function browse(newPage=0, reset=false){
    const token=el('dbsnapshot').value, table=el('dbtable').value;
    if (!token||!table) return;
    const version=++generation;
    busy=true;enable();
    el('dbprev').disabled=true;el('dbnext').disabled=true;
    try{
      const query={act:'browse',token,table,page:newPage,...applied};
      const result=await request(query); // 仅获取当前页和额外一行，避免全表计数。
      if(version!==generation)return;
      const tbl=document.createElement('table'), colgroup=document.createElement('colgroup');
      // 多字段表不再挤进容器：普通列至少 160px，长文本列 300px，超出后横向滚动。
      const widths=result.columns.map((name,index)=>result.rows.some(row=>String(row[index]??'').length>80)?300:Math.max(160,Math.min(300,name.length*9+24)));
      tbl.style.cssText='table-layout:fixed;min-width:100%;border-collapse:collapse;width:'+widths.reduce((sum,width)=>sum+width,0)+'px';
      widths.forEach(width=>{const col=document.createElement('col');col.style.width=width+'px';colgroup.append(col);});tbl.append(colgroup);
      const head=document.createElement('thead'), tr=document.createElement('tr');
      result.columns.forEach(name=>tr.append(node('th',name)));head.append(tr);tbl.append(head);
      const body=document.createElement('tbody');
      result.rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(value=>{
        const text=value===null?'NULL':String(value), cell=document.createElement('td');
        // 长字段默认折叠，展开内容仍用纯文本，防止 JSON 把整页撑成一行。
        if(text.length>120 || text.includes('\n')){
          const details=document.createElement('details');
          details.append(node('summary',text.slice(0,80).replace(/\s+/g,' ')+'…（展开）'));
          details.append(node('pre',text));cell.append(details);
        }else cell.textContent=text;
        tr.append(cell);
      });body.append(tr);});tbl.append(body);el('dbdata').replaceChildren(tbl);
      if(!result.rows.length)el('dbdata').append(node('p','没有符合条件的数据'));
      el('dbschema').textContent=result.schema.map(c=>c.name+'  '+(c.type||'未声明类型')+(c.pk?'  主键':'')+(c.notnull?'  非空':'')).join('\n');
      if(reset){options('dbfield',result.columns,'请选择');options('dbsort',result.columns,'默认顺序');}
      page=result.page;current=query;
      el('dbpage').textContent='第 '+(page+1)+' 页 · 本页 '+result.rows.length+' 行';
      el('dbprev').disabled=page===0;el('dbnext').disabled=!result.more;
    }catch(error){clearData();status(error.message);}
    finally{busy=false;enable();}
  }
  function choose(){
    generation++;clearData();
    const snapshot=snapshots.get(el('dbsnapshot').value);
    options('dbtable',snapshot?snapshot.tables:[],'选择数据表');
    options('dbfield',[],'请选择');options('dbsort',[],'默认顺序');
    el('dbvalue').value='';
    el('dbinfo').textContent=snapshot ? snapshot.name+' · '+snapshot.machine+' · 快照时间 '+snapshot.at+' · '+(snapshot.size/1024/1024).toFixed(2)+' MiB' : '';
    enable();
  }
  el('dball').onclick=()=>root.querySelectorAll('#dbsources input').forEach(c=>{c.checked=true;});
  el('dbnone').onclick=()=>root.querySelectorAll('#dbsources input').forEach(c=>{c.checked=false;});
  el('dbcapture').onclick=async()=>{
    const selected=Array.from(root.querySelectorAll('#dbsources input:checked'),c=>c.value);
    if(!selected.length){status('请至少选择一个数据库');return;}
    busy=true;batch=[];enable();const messages=[];
    try{
      for(const name of selected){
        status(messages.concat(name+'：正在生成并下载快照…').join('\n'));
        try{
          const snapshot=await request({act:'capture',name});snapshots.set(snapshot.token,snapshot);
          batch.push(snapshot.token);
          const option=node('option',snapshot.name+' · '+snapshot.at);option.value=snapshot.token;
          el('dbsnapshot').append(option);el('dbsnapshot').value=snapshot.token;
          messages.push(name+'：成功，'+snapshot.tables.length+' 张表');
        }catch(error){messages.push(name+'：失败，'+error.message);}
      }
      status(messages.join('\n'));choose();
    }finally{busy=false;enable();}
  };
  el('dbsnapshot').onchange=choose;
  el('dbtable').onchange=()=>{
    applied={field:'',value:'',start:'',end:'',sort:'',desc:false};el('dbvalue').value='';
    el('dbstart').value='';el('dbend').value='';
    el('dbdirection').value='asc';clearData();enable();browse(0,true);
  };
  el('dbapply').onclick=()=>{
    applied={field:el('dbfield').value,value:el('dbvalue').value,start:el('dbstart').value,end:el('dbend').value,sort:el('dbsort').value,desc:el('dbdirection').value==='desc'};
    browse(0); // 新筛选总是回到第一页，分页继续使用已应用条件。
  };
  el('dbprev').onclick=()=>browse(Math.max(0,page-1));
  el('dbnext').onclick=()=>browse(page+1);
  el('dbdownload').onclick=async()=>{
    const snapshot=snapshots.get(el('dbsnapshot').value);if(!snapshot)return;
    try{
      const response=await fetch('/api/database/download?token='+encodeURIComponent(snapshot.token));
      if(!response.ok)throw Error((await response.json()).err);
      download(await response.blob(),snapshot.name);
    }catch(error){status(error.message);}
  };
  el('dbcsv').onclick=async()=>{
    if(!current){status('请先查询数据表');return;}
    try{
      const blob=await request({...current,act:'csv'},true); // 导出当前已应用筛选的全部结果。
      download(blob,'database-result.csv');
    }catch(error){status(error.message);}
  };
  el('dblist').onclick=async()=>{
    busy=true;enable();status('正在读取车机文件状态…');
    try{
      const result=await request({act:'list'});
      result.files.forEach(file=>{
        const span=Array.from(root.querySelectorAll('[data-database]')).find(n=>n.dataset.database===file.name);
        if(span)span.textContent=' '+file.name+' · '+(file.err?'不可读取':(file.size/1024/1024).toFixed(2)+' MiB · '+new Date(file.mtime*1000).toLocaleString());
      });
      status(result.machine+'：文件状态已更新');
    }catch(error){status(error.message);}finally{busy=false;enable();}
  };
  el('dbzip').onclick=async()=>{
    busy=true;enable();
    try{download(await request({act:'zip',tokens:batch},true),'database-snapshots-'+Date.now()+'.zip');}
    catch(error){status(error.message);}finally{busy=false;enable();}
  };
  enable();
})();
