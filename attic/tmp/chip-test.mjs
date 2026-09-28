import puppeteer from 'puppeteer-core';
const origin = process.env.ZIM_ORIGIN;
const VW = +(process.env.VW || 390), VH = +(process.env.VH || 844);
const b = await puppeteer.launch({executablePath: process.env.CHROME_PATH,
  args:['--no-sandbox','--disable-dev-shm-usage'], headless:'new'});
const p = await b.newPage();
await p.emulate({viewport:{width:VW,height:VH,isMobile:true,hasTouch:true,deviceScaleFactor:3},
  userAgent:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'});
const errs=[]; p.on('pageerror',e=>errs.push(String(e).slice(0,120)));
await p.goto(`${origin}/index.html`,{waitUntil:'domcontentloaded',timeout:90000});
await p.waitForSelector('canvas.maplibregl-canvas',{timeout:90000});
await new Promise(r=>setTimeout(r,6000));
const info = await p.evaluate(()=>{
  const c=document.getElementById('find-chips');
  if(!c) return {err:'no #find-chips'};
  const chips=[...c.querySelectorAll('.find-chip')];
  const r=c.getBoundingClientRect();
  const out={count:chips.length, rect:{t:+r.top.toFixed(1),l:+r.left.toFixed(1),w:+r.width.toFixed(1),h:+r.height.toFixed(1)},
    scrollW:c.scrollWidth, clientW:c.clientWidth,
    cs:{pe:getComputedStyle(c).pointerEvents, ox:getComputedStyle(c).overflowX, mask:getComputedStyle(c).webkitMaskImage!=='none'},
    topInset:getComputedStyle(document.documentElement).getPropertyValue('--top-inset').trim()};
  if(chips.length){
    const cr=chips[0].getBoundingClientRect();
    out.first={label:chips[0].textContent.trim().slice(0,20),t:+cr.top.toFixed(1),l:+cr.left.toFixed(1),w:+cr.width.toFixed(1),h:+cr.height.toFixed(1)};
    // who actually receives a tap at the chip's centre?
    const el=document.elementFromPoint(cr.left+cr.width/2, cr.top+cr.height/2);
    out.hit = el ? (el.className && typeof el.className==='string' ? el.tagName+'.'+el.className.split(' ')[0] : el.tagName+'#'+el.id) : 'null';
    out.hitIsChip = !!(el && el.closest && el.closest('.find-chip'));
  }
  return out;
});
let clicked='n/a';
if(info.count){
  try{
    const c=await p.$('#find-chips .find-chip');
    await c.tap();
    await new Promise(r=>setTimeout(r,4000));
    clicked=await p.evaluate(()=>{
      const r=document.getElementById('search-results');
      const vis=r&&getComputedStyle(r).display!=='none'&&r.querySelectorAll('li').length;
      const act=document.querySelector('#find-chips .find-chip.active,#find-chips .find-chip[aria-pressed="true"]');
      return `results_li=${vis||0} active=${!!act}`;
    });
  }catch(e){clicked='TAP ERROR '+String(e).slice(0,60);}
}
console.log(JSON.stringify({...info,clicked,errs:errs.slice(0,3)},null,1));
await b.close();
