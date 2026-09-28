import puppeteer from 'puppeteer-core';
const origin=process.env.ZIM_ORIGIN;
const b=await puppeteer.launch({executablePath:process.env.CHROME_PATH,args:['--no-sandbox','--disable-dev-shm-usage'],headless:'new'});
const p=await b.newPage();
await p.emulate({viewport:{width:390,height:844,isMobile:true,hasTouch:true,deviceScaleFactor:3},
  userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'});
const net=[],warn=[];
p.on('response',r=>{const u=r.url().replace(origin,''); if(/search-data/.test(u)) net.push(r.status()+' '+u+' '+(r.headers()['content-length']||'?'));});
p.on('console',m=>{if(m.type()!=='log')warn.push(m.type()+': '+m.text().slice(0,120));});
p.on('pageerror',e=>warn.push('pageerror: '+String(e).slice(0,120)));
await p.goto(`${origin}/index.html`,{waitUntil:'domcontentloaded',timeout:90000});
await p.waitForSelector('canvas.maplibregl-canvas',{timeout:90000});
await new Promise(r=>setTimeout(r,6000));
console.log('placeholder:', await p.evaluate(()=>document.getElementById('search-input').placeholder));
await p.click('#search-input');
await p.type('#search-input','San Francisco',{delay:80});
let n=0,t0=Date.now();
for(let i=0;i<30;i++){
  await new Promise(r=>setTimeout(r,2000));
  n=await p.evaluate(()=>document.querySelectorAll('#search-results li, #search-results .result, #search-results > div').length);
  if(n>0) break;
}
const detail=await p.evaluate(()=>{
  const r=document.getElementById('search-results');
  return {display:getComputedStyle(r).display, childCount:r.children.length,
          firstTag:r.firstElementChild?r.firstElementChild.tagName+'.'+(r.firstElementChild.className||''):null,
          html:r.innerHTML.slice(0,240)};
});
console.log('results after', ((Date.now()-t0)/1000).toFixed(1)+'s:', n);
console.log('detail:', JSON.stringify(detail));
console.log('search-data fetches:'); net.slice(0,8).forEach(x=>console.log('  '+x));
console.log('warnings:'); warn.slice(0,5).forEach(x=>console.log('  '+x));
await b.close();
