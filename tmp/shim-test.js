const fs=require('fs'), vm=require('vm');
const h=fs.readFileSync('resources/viewer/index.html','utf8');
const m=h.match(/<script>\n\/\* Kiwix iOS safe-area shim[\s\S]*?<\/script>/);
const body=m[0].replace(/^<script>/,'').replace(/<\/script>$/,'');
const IOS='Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148';
const IPAD='Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148';
const AND='Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile';
const DESK='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36';
function run({proto,ua,sh,sw,ih,iw,env,touch=true,coarse=true}){
  const set={},removed=[];
  const style={setProperty:(k,v)=>set[k]=v,removeProperty:k=>removed.push(k)};
  const el=()=>({style:{cssText:''},remove(){},getBoundingClientRect:()=>env||{top:0,height:0}});
  const doc={documentElement:{style},body:{appendChild(){}},createElement:el,addEventListener(){},readyState:'complete'};
  const win={innerHeight:ih,innerWidth:iw,addEventListener(){},visualViewport:null,
             matchMedia:q=>({matches:q.includes('coarse')?coarse:false})};
  const ctx={window:win,document:doc,navigator:{userAgent:ua,platform:'x',maxTouchPoints:touch?5:0},
             screen:{height:sh,width:sw},location:{protocol:proto},Math,matchMedia:win.matchMedia};
  ctx.globalThis=ctx;
  vm.createContext(ctx);
  try{vm.runInContext(body,ctx);}catch(e){return{err:String(e)};}
  return {set,removed};
}
const C=[
 ['iPhone 16 Pro Max 956x440',{proto:'zim:',ua:IOS,sh:956,sw:440,ih:956,iw:440},{'--top-inset':'62px','--safe-bottom':'34px'}],
 ['iPhone 13 mini 812x375',   {proto:'zim:',ua:IOS,sh:812,sw:375,ih:812,iw:375},{'--top-inset':'62px','--safe-bottom':'34px'}],
 ['UNKNOWN future tall iPhone',{proto:'zim:',ua:IOS,sh:1010,sw:465,ih:1010,iw:465},{'--top-inset':'62px','--safe-bottom':'34px'}],
 ['iPhone SE 667x375 (16:9)', {proto:'zim:',ua:IOS,sh:667,sw:375,ih:667,iw:375},{'--top-inset':'20px','--safe-bottom':'0px'}],
 ['iPad 1024x768',            {proto:'zim:',ua:IPAD,sh:1024,sw:768,ih:1024,iw:768},{'--top-inset':'20px','--safe-bottom':'0px'}],
 ['Android Pixel 915x412',    {proto:'zim:',ua:AND,sh:915,sw:412,ih:915,iw:412},{'--top-inset':'28px','--safe-bottom':'24px'}],
 ['Android tablet 1280x800',  {proto:'zim:',ua:AND,sh:1280,sw:800,ih:1280,iw:800},{'--top-inset':'24px','--safe-bottom':'0px'}],
 ['iOS landscape',            {proto:'zim:',ua:IOS,sh:956,sw:440,ih:440,iw:956},{'--top-inset':'0px','--safe-bottom':'21px'}],
 ['iOS chrome shown',         {proto:'zim:',ua:IOS,sh:956,sw:440,ih:877,iw:440},{'--top-inset':'0px','--safe-bottom':'0px'}],
 ['tiny viewport cap (10%)',  {proto:'zim:',ua:IOS,sh:400,sw:180,ih:400,iw:180},{'--top-inset':'40px','--safe-bottom':'34px'}],
 ['Desktop Kiwix maximised',  {proto:'zim:',ua:DESK,sh:1080,sw:1920,ih:1080,iw:1920,touch:false,coarse:false},'RESET'],
 ['Web https',                {proto:'https:',ua:IOS,sh:956,sw:440,ih:956,iw:440},'RESET'],
 ['real env insets present',  {proto:'zim:',ua:IOS,sh:956,sw:440,ih:956,iw:440,env:{top:59,height:34}},'RESET'],
];
let fail=0;
for(const [n,cfg,want] of C){
  const r=run(cfg);
  if(r.err){console.log('FAIL',n,r.err);fail++;continue;}
  const ok = want==='RESET'
    ? Object.keys(r.set).length===0 && r.removed.includes('--top-inset')
    : JSON.stringify(r.set)===JSON.stringify(want);
  console.log((ok?'PASS':'FAIL'),n.padEnd(28),want==='RESET'?`reset set=${JSON.stringify(r.set)}`:JSON.stringify(r.set));
  if(!ok)fail++;
}
console.log(fail?`\n${fail} FAILED`:`\nall ${C.length} passed`);
process.exit(fail?1:0);
