import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const kitchen={
 stalls:()=>api('/kitchen/stalls'),
 ledger:stall=>api('/kitchen/ledger?stall='+encodeURIComponent(stall)),
 pending:stall=>api('/kitchen/pending'+(stall?'?stall='+encodeURIComponent(stall):'')),
 alerts:stall=>api('/kitchen/alerts'+(stall?'?stall='+encodeURIComponent(stall):'')),
 alert:id=>api('/kitchen/alerts/'+id),
 trail:stall=>api('/kitchen/trail?stall='+encodeURIComponent(stall)),
 adopt:(operation_id,stall,payload)=>api('/kitchen/adoptions',{method:'POST',body:JSON.stringify({operation_id,stall,payload})}),
 batches:(operation_id,stall,payload)=>api('/kitchen/batches',{method:'POST',body:JSON.stringify({operation_id,stall,payload})}),
 confirm:(id,data)=>api('/kitchen/alerts/'+id+'/confirm',{method:'POST',body:JSON.stringify(data)}),
 revoke:(id,data)=>api('/kitchen/alerts/'+id+'/revoke',{method:'POST',body:JSON.stringify(data)}),
 operation:id=>api('/kitchen/operations/'+encodeURIComponent(id)),
 recover:operation_id=>api('/kitchen/operations/recover',{method:'POST',body:JSON.stringify({operation_id})}),
};
