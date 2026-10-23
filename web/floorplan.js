import { API, MAP_MESSAGES } from './contracts.js';

// The embedded Studio owns geometry. This adapter only bridges navigation and canonical locations.
export function createFloorplan({ request, notify, onExpired }) {
  let root=null, frame=null, ready=false, dirty=false, locationList=null, loading=null, epoch=0, pendingFocus=null;
  const locations=()=>locationList||[];
  function send(message) { frame?.contentWindow?.postMessage(message,window.location.origin); }
  function flushFocus() { if(ready&&pendingFocus!=null){send({type:MAP_MESSAGES.focus,location_id:pendingFocus});pendingFocus=null;} }
  window.addEventListener('message',event=>{
    if(event.origin!==window.location.origin||event.source!==frame?.contentWindow||!event.data||typeof event.data!=='object')return;
    if(event.data.type===MAP_MESSAGES.ready){ready=true;flushFocus();}
    else if(event.data.type===MAP_MESSAGES.dirty&&typeof event.data.dirty==='boolean')dirty=event.data.dirty;
    else if(event.data.type===MAP_MESSAGES.expired)onExpired?.();
  });
  async function refresh() {
    if(loading)return loading;const generation=epoch;
    const pending=request(API.locations).then(data=>{if(generation===epoch)locationList=Array.isArray(data?.locations)?data.locations:[];return locations();}).catch(error=>{if(generation===epoch)notify(error.message,true);return locations();}).finally(()=>{if(loading===pending)loading=null;});
    loading=pending;return pending;
  }
  const ensure=()=>locationList===null?refresh():Promise.resolve(locations());
  function unmount(){frame?.remove();frame=null;root=null;ready=false;dirty=false;}
  function reset(){epoch++;try{const child=frame?.contentWindow;if(child)child.dispatchEvent(new child.Event(MAP_MESSAGES.expired));}catch{/* Child may not have loaded yet. */}send({type:MAP_MESSAGES.expired});unmount();locationList=null;loading=null;pendingFocus=null;}
  function mount(node){
    if(!node)return;if(root===node&&frame?.isConnected)return;
    unmount();root=node;frame=document.createElement('iframe');frame.className='studio-frame';frame.title='家的地圖：家具配置與收納物品';frame.src=API.mapStudio;frame.setAttribute('referrerpolicy','same-origin');root.replaceChildren(frame);ensure();
  }
  async function focusItem(item){
    await ensure();const id=Number(item.location_id)||Number(locations().find(l=>l.name===item.location)?.id);
    if(!Number.isSafeInteger(id)||id<=0){notify('這個物品尚未設定庫存位置。');return;}
    pendingFocus=id;flushFocus();
  }
  function canLeave(){return !dirty||window.confirm('地圖配置還沒有保存。離開將捨棄這份草稿，確定離開？');}
  return {mount,unmount,reset,refresh,ensure,locations,focusItem,canLeave,get dirty(){return dirty;}};
}
