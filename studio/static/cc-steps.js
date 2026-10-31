/* Stepped layout: moves the existing controls (same ids, same listeners) into 5 cards. */
(()=>{const $=s=>document.querySelector(s),g=i=>document.getElementById(i);
const wrap=(el,sel)=>el&&(sel?el.closest(sel)||el:el);
function card(n,title,tip){const s=document.createElement('section');s.className='st-card';s.id='st'+n;
 s.innerHTML=`<h2><i>${n}</i>${title}${tip?`<span class=st-tip tabindex=0 data-tip="${tip}">?</span>`:''}</h2><div class=st-body></div>`;return s}
function more(sec,label){const d=document.createElement('details');d.className='st-more';d.innerHTML=`<summary>${label||'More options'}</summary><div class=st-mbody></div>`;sec.appendChild(d);return d.querySelector('.st-mbody')}
const put=(box,els)=>els.forEach(e=>{if(e&&e!==box&&!e.contains(box))box.appendChild(e)});
function presetCards(){const s=g('style_preset');if(!s||s._cards)return;s._cards=1;const box=document.createElement('div');box.className='st-pcards';
 const draw=()=>{box.innerHTML='';[...s.options].forEach(o=>{const b=document.createElement('button');b.type='button';b.className='st-pc'+(o.value==s.value?' on':'');
  const t=o.textContent.trim();b.innerHTML=`<b>${t.replace(/\s*\(.*$/,'')}</b>${/\(/.test(t)?`<small>${t.replace(/^[^(]*\(/,'').replace(/\)$/,'')}</small>`:''}`;b.title=t;
  b.onclick=()=>{s.value=o.value;s.dispatchEvent(new Event('change',{bubbles:true}));draw()};box.appendChild(b)})};
 s.addEventListener('change',draw);new MutationObserver(draw).observe(s,{childList:true});draw();s.classList.add('st-hidden');s.parentElement.insertBefore(box,s)}
function credits(){const all=[...document.querySelectorAll('#cc-presets div,#cc-presets span')].reverse();const e=all.find(x=>/Est\. Flow credits/.test(x.textContent)&&x.children.length<8);
 if(!e)return'';const m=e.textContent.match(/video:\s*([\d–\-]+)/);return m?m[1]:''}
function bar(goId,lenFn){const go=g(goId);if(!go)return;const b=document.createElement('div');b.className='st-bar';
 b.innerHTML=`<div class=st-stat><span title="Estimated video length">⏱ <b id=st_len>–</b></span><span title="Estimated Google Flow credits (images use the free daily quota)">💳 <b id=st_cr>–</b> Flow credits</span></div><button type=button class=st-stop id=st_stop hidden>■ Stop</button><button type=button class=st-go id=st_go>✦ Generate</button>`;
 document.body.appendChild(b);document.body.classList.add('has-stbar');
 g('st_go').onclick=()=>go.click();g('st_stop').onclick=()=>{const s=[...document.querySelectorAll('.stopbtn')].find(x=>!x.disabled&&!b.contains(x));s&&s.click()};
 const tick=()=>{g('st_go').disabled=go.disabled;g('st_go').title=go.disabled?(goId=='lgo'?'Plan chapters first':'Working…'):'';g('st_len').textContent=lenFn()||'–';const c=credits();g('st_cr').textContent=c||'0';
  g('st_stop').hidden=![...document.querySelectorAll('.stopbtn')].some(x=>!x.disabled&&!b.contains(x))};setInterval(tick,700);tick()}
function tips(root){root.querySelectorAll('.cc-sub,.hint').forEach(p=>{const t=p.textContent.trim();if(t.length>110&&!p.closest('.st-more')&&!p.id){const s=document.createElement('span');s.className='st-tip';s.tabIndex=0;s.dataset.tip=t;s.textContent='i';p.replaceWith(s)}})}
function shorts(){const main=g('create');if(!main||!g('go'))return;const prompt=(g('prompt')&&g('prompt').closest('.cc-prompt'))||[...main.querySelectorAll('.cc-prompt')].find(x=>!x.closest('#ideas'));if(!prompt)return;const ch=id=>wrap(g(id),'.cc-chip');
 const h=main.querySelector('.cc-h1'),sub=h&&h.nextElementSibling;if(sub&&sub.classList.contains('cc-sub')){sub.innerHTML='Shorts · 9:16 for TikTok, Reels and YouTube Shorts · need 16:9? <a href="/longform" style="color:#ff6b6b">Long-form</a>'}
 const grid=document.createElement('div');grid.className='st-wrap';prompt.parentElement.insertBefore(grid,prompt);
 const c=[card(1,'Script / idea','Describe a topic and we write it, or paste your own script. Pick the length here.'),card(2,'Visuals','Google Flow AI images and clips, stock footage, or your own uploads.'),card(3,'Voice & music'),card(4,'Subtitles','Pick a preset; fine-tune under Advanced.'),card(5,'Brand & output')];
 c.forEach(x=>grid.appendChild(x));const B=c.map(x=>x.querySelector('.st-body'));
 const row=(els,cls)=>{const r=document.createElement('div');r.className='st-row '+(cls||'');put(r,els.filter(Boolean));return r};
 put(B[0],[g('mode')&&g('mode').parentElement,g('prompt'),row([ch('length')]),wrap(g('terms'),'label'),$('.sc-tools'),g('sc_hookp')]);
 put(B[1],[row([ch('visuals'),ch('ai_style'),ch('engine')]),g('cc-presets')]);presetCards();
 const m2=more(c[1],'Characters & per-sentence footage');put(m2,[g('ch-panel'),g('sc_scp'),g('sc_flowp')]);
 const fld=id=>wrap(g(id),'.cc-field');
 put(B[2],[row([ch('voice'),ch('music')]),fld('rate'),g('elnote')]);
 put(B[3],[row([ch('subs')]),fld('presets'),g('sp-frame')&&g('sp-frame').closest('.cc-field'),row([fld('sub_font'),wrap(g('sub_bold'),'.cc-field')])]);
 const m4=more(c[3],'Advanced subtitle settings');const adv=prompt.querySelector('details.cc-adv:not(#ch-panel):not(#sa-panel)');
 if(adv)[...adv.querySelectorAll('.cc-grid2>*')].forEach(e=>{if(!B[3].contains(e)&&!e.contains(B[3])&&e.id!='ch-panel')m4.appendChild(e)});if(g('sa-panel'))m4.appendChild(g('sa-panel'));
 put(B[4],[row([ch('aspect')]),g('eta')]);const m5=more(c[4]);put(m5,[fld('clip')]);
 const warn=g('eta')&&g('eta').nextElementSibling;if(warn&&/free server/i.test(warn.textContent))B[4].appendChild(warn);
 grid.after(g('msg'));const goB=g('go');goB.classList.add('st-hidden');B[4].appendChild(goB);
 [...prompt.querySelectorAll('.cc-chip,.cc-bar,.cc-field,details')].length||0;prompt.querySelectorAll('.cc-bar,details.cc-adv').forEach(d=>{if(!d.querySelector('select,input,textarea,button'))d.remove()});
 if(!prompt.querySelector('select,input,textarea,button,details'))prompt.remove();else{prompt.classList.add('st-leftover');B[4].appendChild(prompt)}
 wizard(grid,c,null);tips(grid);bar('go',()=>{const l=g('length');return l?l.options[l.selectedIndex].text:''})}
function longf(){const main=g('cc-long');if(!main||!g('lgo'))return;const S=i=>g('s'+i);const nav=main.querySelector('.stepnav');
 const grid=document.createElement('div');grid.className='st-wrap';S(1).before(grid);
 const c=[card(1,'Script','Paste the full narration. Blank lines between paragraphs help the chapter split.'),card(2,'Visuals','Google Flow AI (director picks image or video per shot) or stock footage.'),card(3,'Voice & music'),card(4,'Subtitles','Live preview updates as you change settings.'),card(5,'Brand & output','Plan chapters, name them, then render.')];
 c.forEach(x=>grid.appendChild(x));const B=c.map(x=>x.querySelector('.st-body'));const kids=s=>[...s.children].filter(e=>e.tagName!='H2');
 put(B[0],kids(S(1)));
 const v=kids(S(3));put(B[1],v.filter(e=>e.id!='ch-panel'&&e.id!='cc-presets'));if(g('cc-presets'))B[1].appendChild(g('cc-presets'));presetCards();put(more(c[1],'Characters'),[g('ch-panel')]);
 put(B[2],kids(S(2)));const mm=more(c[2],'Music options');put(mm,kids(S(4)));
 const goB=g('lgo'),gm=g('lgomsg');put(B[3],kids(S(6)).filter(e=>e!==goB&&e!==gm));
 const sw=g('lsubwrap');if(sw){const m=more(c[3],'Advanced subtitle settings');[...sw.children].forEach(e=>{if(!e.querySelector('#presets,#sub_font,#sub_bold,#sp-frame')&&!e.matches('#presets'))m.appendChild(e)});
  const oa=B[3].querySelector('details.cc-adv');if(oa){[...oa.children].filter(e=>e.tagName!='SUMMARY').forEach(e=>m.appendChild(e));oa.remove()}}
 const h=S(5).querySelector('h2 button');put(B[4],[h,...kids(S(5)),goB,gm]);goB.classList.add('st-hidden');
 [1,2,3,4,5,6].forEach(i=>S(i)&&S(i).remove());c.forEach((x,i)=>x.id='s'+(i+1));
 if(nav)nav.innerHTML=['Script','Visuals','Voice & music','Subtitles','Brand & output'].map((t,i)=>`<a href="#s${i+1}">${i+1} ${t}</a>`).join('');
 wizard(grid,c,nav);tips(grid);bar('lgo',()=>{const t=(g('lstat')||{}).textContent||'';const m=t.match(/≈[\d.]+ min/);return m?m[0]:''})}
function wizard(grid,c,nav){const names=c.map(x=>x.querySelector('h2').childNodes[1].textContent.trim());
 if(!nav){nav=document.createElement('nav');grid.before(nav)}nav.className='st-tabs';nav.innerHTML=names.map((t,i)=>`<a href="#${c[i].id}"><i>${i+1}</i>${t}</a>`).join('');grid.classList.add('st-wiz');
 const T=[...nav.children];let k=0;const go=i=>{k=Math.max(0,Math.min(c.length-1,i));c.forEach((x,j)=>x.classList.toggle('st-on',j==k));T.forEach((t,j)=>{t.classList.toggle('on',j==k);t.classList.toggle('dn',j<k)});if(nav.getBoundingClientRect().top<0)nav.scrollIntoView({block:'start'})};
 c.forEach((x,j)=>{const w=document.createElement('div');w.className='st-wnav';w.innerHTML=(j?'<button type=button class=st-back>← Back</button>':'<span></span>')+(j<c.length-1?`<button type=button class=st-next>Next: ${names[j+1]} →</button>`:'');x.appendChild(w);
  w.querySelectorAll('button').forEach(b=>b.onclick=()=>go(b.classList.contains('st-next')?j+1:j-1))});
 T.forEach((t,j)=>t.onclick=e=>{e.preventDefault();go(j)});go(Math.max(0,c.findIndex(x=>'#'+x.id==location.hash)))}
const run=()=>{try{shorts();longf()}catch(e){console.error('cc-steps',e)}};
if(document.readyState=='complete')setTimeout(run,0);else addEventListener('load',()=>setTimeout(run,0))})();
