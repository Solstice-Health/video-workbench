export const shapes = [
  { id:'rect',type:'shape',shape:'rect',box:[{f:10,v:[8,8,70,30]},{f:14,v:[30,8,80,30],ease:'inout'}],fill:[{f:10,v:'#ff0000'},{f:14,v:'#00ff00'}],radius:5,
    opacity:[{f:10,v:0.6},{f:14,v:1}],rotate:[{f:10,v:0},{f:14,v:12}],stroke:{width:2,color:[{f:10,v:'#ffffff'},{f:14,v:'#aabbcc'}]} },
  { id:'circle',type:'shape',shape:'circle',box:[110,8,35,24],gradient:['#112233','#ddeeff'],gradient_angle:45,fill:'#123456' },
  { id:'line',type:'shape',shape:'line',box:[8,50,65,0],fill:'#abcdef',progress:true },
  { id:'path',type:'shape',shape:'path',box:[85,42,30,25],viewbox:[30,25],d:'M0 20 Q15 0 30 20',draw:[{f:10,v:0},{f:14,v:1}],stroke:{width:2,color:'#ffffff',cap:'round'},fill:'#f0304080',gradient:['#f0304080','#3344ff'],gradient_angle:30 },
  { id:'svg',type:'shape',shape:'svg',box:[128,40,22,25],viewbox:[30,30],paths:[{d:'M0 0 L30 0 L15 30 Z',fill:'#ff9900',opacity:0.9,translate:[0,0],stroke:{width:1,color:'#ffff00',dash:[2,1]}}] },
  { id:'group',type:'shape',shape:'path',box:[160,10,22,25],viewbox:[30,30],draw:[{f:10,v:0.4},{f:14,v:1}],paths:[{d:'M0 0 L30 0 L15 30 Z',fill:'#00ff99',stroke:{width:2,color:'#ffaa33'},opacity:0.8,translate:[1,0]}] },
];
export const style = { font:'Inter',weight:700,size:19,color:'#ffffff',align:'left',valign:'top',line_height:1.2,letter_spacing:0,uppercase:false,shadow:true,padding:1,radius:2 };
export function props(full = false) { return { width:192,height:128,fps:24,durationInFrames:4,offset:10,total:20,layers:[...structuredClone(shapes), ...(full ? [
  {id:'text',type:'text',copy_id:'t',text:'Hello^{1} </script>',box:[4,72,184,25],style:{...style,accent_bar:'#aabbcc',stroke:{width:0.2,color:'#000000'}},animate:{enter:'pop',exit:'none',frames:3},span:[10,20]},
  {id:'caption',type:'captions',box:[4,102,180,24],style:{...style,size:15,highlight:'#ff9900',muted:'#999999'},mode:'karaoke',pages:[{copy_id:'c',from:10,to:20,lines:[[{text:'one',from:10,to:12},{text:'two',from:12,to:18}]]}]},
] : [])] }; }
