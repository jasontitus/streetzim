import puppeteer from 'puppeteer-core';
const origin = process.env.ZIM_ORIGIN;
const b = await puppeteer.launch({executablePath: process.env.CHROME_PATH,
  args:['--no-sandbox','--disable-dev-shm-usage'], headless:'new'});
const p = await b.newPage();
await p.emulate({viewport:{width:390,height:844,isMobile:true,hasTouch:true,deviceScaleFactor:3},
  userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'});
const logs=[],errs=[];
p.on('console',m=>{const t=m.text(); if(/chip|find|error|warn|fail/i.test(t)) logs.push(m.type()+': '+t.slice(0,140));});
p.on('pageerror',e=>errs.push(String(e).slice(0,140)));
const reqFail=[];
p.on('requestfailed',r=>{ if(/chip/i.test(r.url())) reqFail.push(r.url().split('/').slice(-2).join('/')); });
const resp=[];
p.on('response',r=>{ if(/chip/i.test(r.url())) resp.push(r.status()+' '+r.url().split('/').slice(-2).join('/')); });
await p.goto(`${origin}/index.html`,{waitUntil:'domcontentloaded',timeout:90000});
await p.waitForSelector('canvas.maplibregl-canvas',{timeout:90000});
await new Promise(r=>setTimeout(r,7000));
const before = await p.evaluate(()=>({
  chips:document.querySelectorAll('#find-chips .find-chip').length,
  sources:Object.keys(window.map&&map.getStyle?map.getStyle().sources||{}:{}).length,
}));
await p.evaluate(()=>{ const c=document.querySelector('#find-chips .find-chip'); c && c.click(); });
await new Promise(r=>setTimeout(r,8000));
const after = await p.evaluate(()=>{
  const on=document.querySelectorAll('#find-chips .find-chip.on').length;
  const toast=[...document.querySelectorAll('div')].map(d=>d.textContent||'')
      .filter(t=>/Couldn.t load|Loading/i.test(t)).slice(0,2);
  const si=document.getElementById('search-input');
  let srcs=[],layers=0;
  try{ const st=map.getStyle(); srcs=Object.keys(st.sources).filter(s=>/chip|find|explore/i.test(s));
       layers=st.layers.filter(l=>/chip|find|explore/i.test(l.id)).length; }catch(e){}
  return {on, toast, placeholder:si?si.placeholder:null, chipSources:srcs, chipLayers:layers};
});
console.log(JSON.stringify({before,after,chipResponses:resp.slice(0,6),reqFail:reqFail.slice(0,4),
  logs:logs.slice(0,6),errs:errs.slice(0,3)},null,1));
await b.close();
