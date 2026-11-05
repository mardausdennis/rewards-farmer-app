// Synthetic DOM for the exact production snapshots. No browser or network.
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let effects = 0;
const forbidden = () => {effects++; throw Error('unexpected effect');};
function split(text, delimiter) {
 const parts=[];let depth=0,quote=null,start=0;
 for(let i=0;i<text.length;i++){const c=text[i];if(quote){if(c===quote)quote=null;continue;}if(c==='"'||c==="'"){quote=c;continue;}if(c==='['||c==='(')depth++;if(c===']'||c===')')depth--;if(!depth&&c===delimiter){if(text.slice(start,i).trim())parts.push(text.slice(start,i).trim());start=i+1;}}
 if(text.slice(start).trim())parts.push(text.slice(start).trim());return parts;
}
class Node {
 constructor(raw,parent=null){this.tagName=raw.tag.toUpperCase();this.attrs=raw.attrs||{};this.id=this.attrs.id||'';this.parentElement=parent;this.own=raw.text||'';this.children=(raw.children||[]).map(x=>new Node(x,this));this.shadowRoot=null;}
 hidden(){return this.attrs.hidden!==undefined||this.attrs['aria-hidden']==='true'||/display\s*:\s*none/.test(this.attrs.style||'')||!!this.parentElement?.hidden();}
 get innerText(){return this.hidden()?'':this.own+this.children.map(e=>e.innerText).filter(Boolean).join('\n');}
 get textContent(){return this.own+this.children.map(e=>e.textContent).filter(Boolean).join('\n');}
 getAttribute(name){return this.attrs[name]??null;}
 getBoundingClientRect(){return this.hidden()?{width:0,height:0}:{width:100,height:20};}
 getClientRects(){return this.hidden()?[]:[this.getBoundingClientRect()];}
 matches(selector){
  return split(selector,',').some(part=>{const tokens=split(part,' ');let current=this,index=tokens.length-1;if(!single(current,tokens[index]))return false;while(--index>=0){current=current.parentElement;while(current&&!single(current,tokens[index]))current=current.parentElement;if(!current)return false;}return true;});
 }
 closest(selector){let node=this;while(node){if(node.matches(selector))return node;node=node.parentElement;}return null;}
 querySelectorAll(selector){
  if(selector.startsWith(':scope > ')){let nodes=[this];for(const token of selector.slice(9).split(' > '))nodes=nodes.flatMap(node=>node.children.filter(child=>single(child,token)));return nodes;}
  const out=[];const visit=node=>{for(const child of node.children){if(child.matches(selector))out.push(child);visit(child);}};visit(this);return out;
 }
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 click(){forbidden();}
}
function single(node,token){
 if(!node||!token)return false;
 let selector=token;
 for(const match of selector.matchAll(/\[([\w-]+)(?:(\*=|\$=|\^=|=)["']?([^\]"']*?)["']?(?:\s+(i))?)?\]/g)){
  let value=node.getAttribute(match[1]),expected=match[3];if(value===null)return false;if(match[4]){value=value.toLowerCase();expected=expected.toLowerCase();}if(match[2]==='='&&value!==expected)return false;if(match[2]==='*='&&!value.includes(expected))return false;if(match[2]==='$='&&!value.endsWith(expected))return false;if(match[2]==='^='&&!value.startsWith(expected))return false;
 }
 selector=selector.replace(/\[[^\]]+\]/g,'');
 const id=selector.match(/#([\w-]+)/);if(id&&node.id!==id[1])return false;
 const tag=selector.match(/^[\w*-]+/);return !tag||tag[0]==='*'||node.tagName.toLowerCase()===tag[0];
}
const document=new Node(input.tree);document.readyState=input.ready_state||'complete';
const where=new URL(input.url||'https://rewards.bing.com/earn');
const location={hostname:where.hostname,pathname:where.pathname,assign:forbidden,replace:forbidden};
const window={location,innerWidth:1896,innerHeight:940};
const getComputedStyle=e=>({display:e.hidden()?'none':'block',visibility:'visible',opacity:'1'});
const result=vm.runInNewContext('(function(){'+input.script+'}).apply(null,args)',{document,location,window,getComputedStyle,args:input.args||[],fetch:forbidden},{timeout:2000});
process.stdout.write(JSON.stringify({result,effects}));
