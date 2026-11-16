export function route(){
 const [path,query='']=location.hash.replace(/^#\//,'').split('?');
 const [page='overview',...rest]=path.split('/');
 return {page,id:rest.at(-1)||undefined,parts:rest,params:new URLSearchParams(query)};
}
