import puppeteer from 'puppeteer-core';
const origin = process.env.ZIM_ORIGIN;
const b = await puppeteer.launch({executablePath: process.env.CHROME_PATH,
  args:['--no-sandbox','--disable-dev-shm-usage'], headless:'new'});
const p = await b.newPage();
await p.emulate({viewport:{width:390,height:844,isMobile:true,hasTouch:true,deviceScaleFactor:3},
  userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'});
const warns=[];
p.on('console',m=>{if(m.type()!=='log') warns.push(m.type()+': '+m.text().slice(0,160));});
p.on('pageerror',e=>warns.push('pageerror: '+String(e).slice(0,160)));
await p.goto(`${origin}/index.html`,{waitUntil:'domcontentloaded',timeout:90000});
await p.waitForSelector('canvas.maplibregl-canvas',{timeout:90000});
await new Promise(r=>setTimeout(r,7000));
warns.length=0;
await p.evaluate(()=>{const c=document.querySelector('#find-chips .find-chip'); c&&c.click();});
await new Promise(r=>setTimeout(r,12000));
const dump = await p.evaluate(()=>{
  // find the element whose text is exactly the loading string, walk up to its panel
  let node=[...document.querySelectorAll('*')].find(e=>e.children.length===0 && /Loading entries/i.test(e.textContent||''));
  let panel=node; for(let i=0;i<5&&panel&&panel.parentElement;i++) panel=panel.parentElement;
  const idOf=e=>e?(e.id?'#'+e.id:(e.className&&typeof e.className==='string'?'.'+e.className.split(' ').join('.'):e.tagName)):null;
  return {
    loadingNode: idOf(node),
    panel: idOf(panel),
    panelHTML: panel?panel.outerHTML.slice(0,700):null,
    chipOn: document.querySelectorAll('#find-chips .find-chip.on').length,
    globals: {
      hasLoadChip: typeof window.loadChipOnMap,
      chipCacheSize: (window._findChipCache&&window._findChipCache.size)||'n/a',
    }
  };
});
console.log(JSON.stringify({dump,warns:warns.slice(0,8)},null,1));
await b.close();
