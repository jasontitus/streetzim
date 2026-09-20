import puppeteer from 'puppeteer-core';
const origin = process.env.ZIM_ORIGIN;
const IOS='Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148';
// Real logical viewports. inset = what the shim computes for that screen.
const DEVICES=[
 {n:'iPhone SE 2/3',      w:375,h:667,inset:20},
 {n:'iPhone 13 mini',     w:375,h:812,inset:62},
 {n:'iPhone 14/15/16',    w:390,h:844,inset:62},
 {n:'iPhone 16 Pro',      w:402,h:874,inset:62},
 {n:'iPhone 14 Pro Max',  w:430,h:932,inset:62},
 {n:'iPhone 16 Pro Max',  w:440,h:956,inset:62},
 {n:'legacy 320',         w:320,h:568,inset:20},
];
const CONTROLS=[
 ['search input','#search-input'],
 ['attribution btn','#attr-btn'],
 ['zoom in','.maplibregl-ctrl-zoom-in'],
 ['zoom out','.maplibregl-ctrl-zoom-out'],
 ['geolocate','.maplibregl-ctrl-geolocate'],
];
const b=await puppeteer.launch({executablePath:process.env.CHROME_PATH,
  args:['--no-sandbox','--disable-dev-shm-usage'],headless:'new'});
let fails=0;
for(const d of DEVICES){
  const p=await b.newPage();
  await p.emulate({viewport:{width:d.w,height:d.h,isMobile:true,hasTouch:true,deviceScaleFactor:3},userAgent:IOS});
  // Reproduce the Kiwix condition the shim exists for: full-screen web view,
  // env() reporting zero, so the shim's derived inset is what CSS sees.
  await p.evaluateOnNewDocument(v=>{
    addEventListener('DOMContentLoaded',()=>document.documentElement.style.setProperty('--top-inset',v+'px'));
  },d.inset);
  const bad=[];
  p.on('pageerror',e=>bad.push('pageerror '+String(e).slice(0,80)));
  const R={dev:d.n,size:`${d.w}x${d.h}`,inset:d.inset,checks:[],fail:[]};
  try{
    await p.goto(`${origin}/index.html`,{waitUntil:'domcontentloaded',timeout:90000});
    await p.waitForSelector('canvas.maplibregl-canvas',{timeout:90000});
    await new Promise(r=>setTimeout(r,6000));
    const res=await p.evaluate((inset,CONTROLS)=>{
      const out={hit:{},geom:{},overlap:[],offscreen:[]};
      const vis=e=>{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&+s.opacity>0;};
      for(const [label,sel] of CONTROLS){
        const e=document.querySelector(sel);
        if(!e||!vis(e)){out.hit[label]='absent';continue;}
        const r=e.getBoundingClientRect();
        const el=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
        out.hit[label]= el && (el===e||e.contains(el)||el.contains(e)) ? 'ok' : 'BLOCKED by '+(el?el.tagName+(el.id?'#'+el.id:''):'null');
        out.geom[label]=[+r.top.toFixed(0),+r.left.toFixed(0),+r.width.toFixed(0),+r.height.toFixed(0)];
        if(r.top<inset-1) out.offscreen.push(label+' top='+r.top.toFixed(0)+' < inset '+inset);
        if(r.left<0||r.right>innerWidth+1) out.offscreen.push(label+' horiz out '+r.left.toFixed(0)+'..'+r.right.toFixed(0));
      }
      // pairwise overlap of visible top chrome
      const els=[...document.querySelectorAll('#search-container,#attr-btn,#find-chips,.maplibregl-ctrl-top-right')].filter(vis);
      for(let i=0;i<els.length;i++)for(let j=i+1;j<els.length;j++){
        const a=els[i].getBoundingClientRect(),c=els[j].getBoundingClientRect();
        const ox=Math.min(a.right,c.right)-Math.max(a.left,c.left);
        const oy=Math.min(a.bottom,c.bottom)-Math.max(a.top,c.top);
        if(ox>2&&oy>2&&!els[i].contains(els[j])&&!els[j].contains(els[i]))
          out.overlap.push((els[i].id||els[i].className)+' x '+(els[j].id||els[j].className)+` ${ox.toFixed(0)}x${oy.toFixed(0)}`);
      }
      const rail=document.getElementById('find-chips');
      out.chips = rail?rail.querySelectorAll('.find-chip').length:0;
      out.railScrollable = rail? rail.scrollWidth>rail.clientWidth : false;
      return out;
    },d.inset,CONTROLS);
    for(const [k,v] of Object.entries(res.hit)) if(v!=='ok'&&v!=='absent') R.fail.push(`${k}: ${v}`);
    if(res.overlap.length) R.fail.push('overlap: '+res.overlap.join('; '));
    if(res.offscreen.length) R.fail.push('offscreen: '+res.offscreen.join('; '));
    if(!res.chips) R.fail.push('no chips rendered');
    R.checks.push(`controls ${Object.values(res.hit).filter(v=>v==='ok').length}/${CONTROLS.length} hittable`);
    R.checks.push(`chips ${res.chips}`+(res.railScrollable?' scrollable':' NOT scrollable'));
    // chip tap
    const chip=await p.evaluate(()=>{const c=document.querySelector('#find-chips .find-chip');if(!c)return'none';
      const r=c.getBoundingClientRect();const el=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
      return el&&el.closest('.find-chip')?'hittable':'BLOCKED by '+(el?el.tagName+(el.id?'#'+el.id:''):'null');});
    if(chip!=='hittable') R.fail.push('chip1 '+chip); else {
      await p.evaluate(()=>document.querySelector('#find-chips .find-chip').click());
      await new Promise(r=>setTimeout(r,5000));
      const on=await p.evaluate(()=>document.querySelectorAll('#find-chips .find-chip.on').length);
      R.checks.push('chip tap -> on='+on); if(!on) R.fail.push('chip tap did not activate');
      await p.evaluate(()=>{const c=document.querySelector('#find-chips .find-chip.on');c&&c.click();});
      await new Promise(r=>setTimeout(r,1500));
    }
    // rail swipe proxy
    const sw=await p.evaluate(()=>{const r=document.getElementById('find-chips');if(!r)return'none';
      const a=r.scrollLeft; r.scrollLeft=120; const b=r.scrollLeft; r.scrollLeft=a; return b>a?'scrolls':'STUCK at '+b;});
    R.checks.push('rail scroll: '+sw); if(sw.startsWith('STUCK')) R.fail.push('rail not scrollable');
    // search
    await p.click('#search-input').catch(()=>{});
    await p.type('#search-input','San Francisco',{delay:60});
    for(let i=0;i<15;i++){await new Promise(r=>setTimeout(r,2000));
      if(await p.evaluate(()=>document.querySelectorAll('#search-results .search-result').length))break;}
    const sr=await p.evaluate(()=>{const r=document.getElementById('search-results');
      return {vis:r?getComputedStyle(r).display!=='none':false, n:r?r.querySelectorAll('.search-result').length:0};});
    R.checks.push(`search -> ${sr.n} results`); if(!sr.n) R.fail.push('search returned 0');
    if(sr.n){ const srh=await p.evaluate(()=>{const li=document.querySelector('#search-results .search-result');
        const r=li.getBoundingClientRect(); const el=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
        return el&&(li===el||li.contains(el))?'ok':'BLOCKED by '+(el?el.tagName:'null');});
      R.checks.push('result row: '+srh); if(srh!=='ok') R.fail.push('result row '+srh); }
  }catch(e){ R.fail.push('EXC '+String(e).slice(0,110)); }
  if(bad.length) R.fail.push(...bad);
  await p.close();
  const ok=R.fail.length===0; if(!ok) fails++;
  console.log(`${ok?'PASS':'FAIL'}  ${R.dev.padEnd(18)} ${R.size.padEnd(9)} inset=${R.inset}  ${R.checks.join(' | ')}`);
  for(const f of R.fail) console.log(`        ! ${f}`);
}
console.log(fails?`\n${fails}/${DEVICES.length} devices FAILED`:`\nall ${DEVICES.length} devices passed`);
await b.close();
process.exit(fails?1:0);
