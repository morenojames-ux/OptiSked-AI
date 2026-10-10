(function(){
  const gridEl = document.getElementById('calGrid');
  if(!gridEl) return;
  const sel = document.getElementById('calSectionSelect');
  const msgEl = document.getElementById('calMessage');
  const detailEl = document.getElementById('calDetail');

  const sections = JSON.parse((document.getElementById('cal-sections')||{}).textContent || '[]');
  const events = JSON.parse((document.getElementById('cal-events')||{}).textContent || '[]');
  const cfg = JSON.parse((document.getElementById('cal-grid')||{}).textContent || '{}');
  const DAYS = cfg.days || ['Mon','Tue','Wed','Thu','Fri','Sat'];
  const DAY_START = cfg.day_start || 450;
  const DAY_END = cfg.day_end || 1260;
  const STEP = cfg.step || 30;
  const PX_PER_HOUR = cfg.px_per_hour || 56;
  const PX_PER_MIN = PX_PER_HOUR / 60;

  if(!sections.length) return;

  let currentYear = sections[0].year_level;
  let currentSection = sections[0].section_name;
  let currentDrag = null;
  let suppressClick = false;

  function clamp(v,lo,hi){ return Math.max(lo, Math.min(hi, v)); }
  function toMin(v){ if(!v) return -1; const p=String(v).split(':'); if(p.length<2) return -1; const h=parseInt(p[0],10), m=parseInt(p[1],10); if(isNaN(h)||isNaN(m)) return -1; return h*60+m; }
  function fmtMin(m){ m=((m%1440)+1440)%1440; let h=Math.floor(m/60), mm=m%60; const ap=h>=12?'PM':'AM'; h=h%12; if(h===0) h=12; return h+':'+String(mm).padStart(2,'0')+' '+ap; }
  function fmtRange(a,b){ return fmtMin(a)+' – '+fmtMin(b); }
  function fmt24(m){ return String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0'); }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

  function buildSelector(){
    const years = [];
    sections.forEach(s=>{ if(years.indexOf(s.year_level)<0) years.push(s.year_level); });
    let html = '';
    years.forEach(y=>{
      html += '<optgroup label="'+esc(y)+'">';
      sections.filter(s=>s.year_level===y).forEach(s=>{
        html += '<option value="'+esc(y)+'|'+esc(s.section_name)+'">Section '+esc(s.section_name)+'</option>';
      });
      html += '</optgroup>';
    });
    sel.innerHTML = html;
    sel.value = currentYear+'|'+currentSection;
  }

  function sectionEvents(){
    return events.filter(e=>e.year_level===currentYear && e.section===currentSection);
  }

  function render(){
    const bodyHeight = (DAY_END - DAY_START) * PX_PER_MIN;
    let html = '<div class="cal-head"><div class="cal-corner"></div>';
    DAYS.forEach(d=>{ html += '<div class="cal-day-head">'+d+'</div>'; });
    html += '</div>';
    html += '<div class="cal-body">';
    html += '<div class="cal-times" style="height:'+bodyHeight+'px">';
    for(let m=Math.ceil(DAY_START/60)*60; m<DAY_END; m+=60){
      html += '<div class="cal-time-label" style="top:'+((m-DAY_START)*PX_PER_MIN)+'px">'+fmtMin(m)+'</div>';
    }
    html += '</div><div class="cal-days" style="height:'+bodyHeight+'px">';
    DAYS.forEach(day=>{
      html += '<div class="cal-day-col" data-day="'+day+'">';
      for(let m=DAY_START; m<=DAY_END; m+=60){
        html += '<div class="cal-hour-line" style="top:'+((m-DAY_START)*PX_PER_MIN)+'px"></div>';
      }
      html += '</div>';
    });
    html += '</div></div>';
    gridEl.innerHTML = html;

    sectionEvents().forEach(ev=>{
      const col = gridEl.querySelector('.cal-day-col[data-day="'+ev.day+'"]');
      if(!col) return;
      const start = toMin(ev.start_time), end = toMin(ev.end_time);
      if(start<0 || end<=start) return;
      if(end < DAY_START || start > DAY_END) return;
      const top = (start - DAY_START) * PX_PER_MIN;
      const height = Math.max(20, (end - start) * PX_PER_MIN - 2);
      const div = document.createElement('div');
      div.className = 'cal-event ' + (ev.status==='Draft' ? 'draft' : 'published') + (ev.session_type==='Lab' ? ' lab' : '');
      div.style.top = top + 'px';
      div.style.height = height + 'px';
      div.dataset.id = ev.id;
      div.title = ev.subject_code+' — '+ev.subject_name+'\n'+ev.teacher_name+'\n'+fmtRange(start,end)+'\n'+ev.room+' · '+ev.status;
      div.innerHTML = '<strong>'+esc(ev.subject_code)+'</strong><span class="cal-ev-meta">'+esc(ev.room)+' · '+esc(ev.teacher_name)+'</span>';
      div.addEventListener('pointerdown', onDown);
      div.addEventListener('click', onClick);
      col.appendChild(div);
    });
  }

  function showMsg(kind, text){
    if(!msgEl) return;
    msgEl.className = 'cal-msg ' + (kind==='success' ? 'ok' : 'err');
    msgEl.textContent = text;
    msgEl.style.display = 'block';
    clearTimeout(showMsg._t);
    showMsg._t = setTimeout(()=>{ msgEl.style.display='none'; }, 7000);
  }

  function onDown(e){
    if(e.button!==0) return;
    const el = e.currentTarget;
    const ev = events.find(x=>String(x.id)===el.dataset.id);
    if(!ev) return;
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const ghost = el.cloneNode(true);
    ghost.classList.add('cal-ghost');
    ghost.style.position = 'fixed';
    ghost.style.left = rect.left+'px';
    ghost.style.top = rect.top+'px';
    ghost.style.width = rect.width+'px';
    ghost.style.height = rect.height+'px';
    ghost.style.margin = '0';
    document.body.appendChild(ghost);
    el.classList.add('cal-dragging-src');
    currentDrag = { el, ev, ghost, startX:e.clientX, startY:e.clientY, moved:false, preview:null };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  }

  function onMove(e){
    const d = currentDrag;
    if(!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if(Math.abs(dx)>3 || Math.abs(dy)>3) d.moved = true;

    const daysEl = gridEl.querySelector('.cal-days');
    if(!daysEl) return;
    const daysRect = daysEl.getBoundingClientRect();
    const colWidth = daysRect.width / DAYS.length;
    const colIndex = clamp(Math.floor((e.clientX - daysRect.left)/colWidth), 0, DAYS.length-1);

    const origStart = toMin(d.ev.start_time);
    const dur = toMin(d.ev.end_time) - origStart;
    let newStart = origStart + Math.round(dy / PX_PER_MIN / STEP) * STEP;
    newStart = clamp(newStart, DAY_START, DAY_END - dur);

    d.preview = { day: DAYS[colIndex], start: newStart, end: newStart + dur };
    d.ghost.style.left = (daysRect.left + colIndex*colWidth + 1) + 'px';
    d.ghost.style.width = Math.max(40, colWidth - 2) + 'px';
    d.ghost.style.top = (daysRect.top + (newStart - DAY_START)*PX_PER_MIN) + 'px';
  }

  function onUp(){
    const d = currentDrag;
    if(!d) return;
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    currentDrag = null;
    if(d.ghost) d.ghost.remove();
    d.el.classList.remove('cal-dragging-src');

    if(d.moved){ suppressClick = true; setTimeout(()=>{ suppressClick = false; }, 50); }
    if(!d.moved || !d.preview) return;

    const origStart = toMin(d.ev.start_time);
    if(d.preview.day === d.ev.day && d.preview.start === origStart) return;

    if(d.ev.status === 'Published' &&
       !confirm('This session is published. Move it to ' + d.preview.day + ' ' + fmtRange(d.preview.start, d.preview.end) + '?')){
      return;
    }
    sendMove(d.ev, d.preview.day, d.preview.start, d.preview.end);
  }

  function sendMove(ev, day, start, end){
    fetch('/schedule/'+encodeURIComponent(ev.id)+'/move', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ day:day, start_time:fmt24(start), end_time:fmt24(end) })
    }).then(r=>r.json().then(b=>({status:r.status, body:b})).catch(()=>({status:r.status, body:null})))
      .then(({body})=>{
        if(body && body.ok){
          ev.day = body.day; ev.start_time = body.start_time; ev.end_time = body.end_time;
          showMsg('success', 'Moved ' + ev.subject_code + ' to ' + body.day + ' ' + fmtRange(toMin(body.start_time), toMin(body.end_time)) + '.');
        }else{
          const text = (body && body.errors && body.errors.length) ? body.errors.join(' ') : 'Move rejected.';
          showMsg('error', 'Move rejected: ' + text);
        }
        render();
      })
      .catch(()=>{ showMsg('error', 'Network error while moving the session.'); render(); });
  }

  function onClick(e){
    if(suppressClick) return;
    const el = e.currentTarget;
    const ev = events.find(x=>String(x.id)===el.dataset.id);
    if(!ev || !detailEl) return;
    const start = toMin(ev.start_time), end = toMin(ev.end_time);
    detailEl.style.display = 'block';
    detailEl.innerHTML =
      '<div><strong>'+esc(ev.subject_code)+' — '+esc(ev.subject_name)+'</strong>'+
      '<span class="cal-detail-meta">'+esc(ev.teacher_name)+' · '+ev.day+' '+fmtRange(start,end)+' · '+esc(ev.room)+' · '+esc(ev.session_type)+' · '+esc(ev.status)+'</span></div>'+
      '<a class="secondary-button small" href="/"><i class="fa-solid fa-pen"></i> Edit room / instructor in table view</a>';
  }

  sel.addEventListener('change', function(){
    const parts = (sel.value||'').split('|');
    currentYear = parts[0]; currentSection = parts[1];
    if(detailEl) detailEl.style.display = 'none';
    render();
  });

  buildSelector();
  render();
})();
