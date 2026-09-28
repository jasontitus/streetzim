import puppeteer from 'puppeteer-core';
const origin = process.env.ZIM_ORIGIN;
const b = await puppeteer.launch({executablePath: process.env.CHROME_PATH,
  args:['--no-sandbox','--disable-dev-shm-usage'], headless:'new'});
const p = await b.newPage();
await p.emulate({viewport:{width:390,height:844,isMobile:true,hasTouch:true,deviceScaleFactor:3},
  userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'});
const bad=[], ok=[];
p.on('response',r=>{const u=r.url().replace(origin,''); if(r.status()>=400) bad.push(r.status()+' '+u); else if(/chip|nearby|entries|category/i.test(u)) ok.push(r.status()+' '+u);});
await p.goto(`${origin}/index.html`,{waitUntil:'domcontentloaded',timeout:90000});
await p.waitForSelector('canvas.maplibregl-canvas',{timeout:90000});
await new Promise(r=>setTimeout(r,7000));
bad.length=0;
await p.evaluate(()=>{const c=document.querySelector('#find-chips .find-chip'); c&&c.click();});
await new Promise(r=>setTimeout(r,10000));
const state = await p.evaluate(()=>{
  const t=[...document.querySelectorAll('*')].filter(e=>/Loading entries/i.test(e.textContent||'')&&e.children.length===0);
  const li=document.querySelectorAll('#find-results li, .find-result, [id*=nearby] li').length;
  return {stillLoading:t.length>0, resultRows:li};
});
console.log(JSON.stringify({afterClick_404s:bad.slice(0,10), chipRequests:ok.slice(-8), state},null,1));
await b.close();
