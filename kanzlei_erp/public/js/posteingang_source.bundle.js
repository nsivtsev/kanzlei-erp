/* Confirmed Action projection; Task work fields and browser storage are untouched. */
(() => {
	class TaskSourceClient {
		constructor(transport) { this.transport=transport;this.sequence=0;this.state=null; }
		clear() { this.sequence++;this.state=null; }
		async load(task,active) {
			const sequence=++this.sequence;this.state=null;
			try {
				const result=await this.transport(task);
				if(sequence!==this.sequence || !active())return null;
				this.state=result;return result;
			} catch(error) {
				if(sequence!==this.sequence || !active())return null;
				this.state=null;throw error;
			}
		}
		pageUrl(page) {
			return this.state?.available && Number.isInteger(page) && page>=1 && page<=this.state.original.page_count
				? this.state.preview_url+`#page=${page}` : null;
		}
	}
	const text=(parent,tag,value)=>{const element=document.createElement(tag);element.textContent=value ?? '';parent.append(element);return element;};
	function link(parent,label,url) {
		const element=text(parent,'a',label);element.href=url;element.target='_blank';element.rel='noopener noreferrer';element.className='btn btn-default btn-xs mr-2 mb-2';return element;
	}
	async function transport(task) {
		return (await frappe.call({method:'kanzlei_erp.posteingang_documents.get_task_source',args:{task}})).message;
	}
	let current=null;
	function attach(frm) {
		if(frm.is_new())return;
		if(current) {current.client.clear();current.wrapper?.closest('.form-dashboard-section').remove();}
		const doc=frm.doc,client=new TaskSourceClient(transport),context={client,wrapper:null};current=context;
		const active=()=>current===context && frm.doc===doc && frappe.get_route_str()===`Form/Task/${doc.name}`;
		async function refresh() {
			context.wrapper?.empty();
			try {
				const state=await client.load(doc.name,active);
				if(!state || !active())return;
				if(!state.available){context.wrapper?.closest('.form-dashboard-section').remove();context.wrapper=null;return;}
				const box=context.wrapper || frm.dashboard.add_section('',__('Posteingang source'),'posteingang-source');context.wrapper=box;
				box.empty();
				if(state.letter.quarantined)text(box,'p',__('This original is quarantined for administrative review.'));
				const letter=text(box,'button',__('Open incoming letter')+': '+state.letter.name);letter.type='button';letter.className='btn btn-default btn-xs mr-2 mb-2';letter.addEventListener('click',()=>frappe.set_route('Form','Incoming Letter',state.letter.name));
				text(box,'p',state.original.file_name);
				link(box,__('Open original PDF'),state.preview_url);link(box,__('Download'),state.download_url);
				text(box,'p',state.action.external_date ? __('Confirmed external deadline')+': '+state.action.external_date : __('External deadline is not confirmed'));
				if(state.action.external_date_basis)text(box,'p',state.action.external_date_basis);
				for(const evidence of state.action.evidence) {
					const line=text(box,'div','');text(line,'p',__(evidence.verification));text(line,'p',evidence.quote);
					const url=client.pageUrl(evidence.page);if(url)link(line,__('Page {0}',[evidence.page]),url);
				}
				const again=text(box,'button',__('Refresh source'));again.type='button';again.className='btn btn-default btn-xs';again.addEventListener('click',refresh);
			} catch {
				if(!active())return;
				context.wrapper?.empty();
				if(context.wrapper)text(context.wrapper,'p',__('Document is no longer available'));
			}
		}
		refresh();
	}
	frappe.router?.on('change',()=>{
		if(current && !frappe.get_route_str().startsWith('Form/Task/')){current.client.clear();current.wrapper?.empty();current=null;}
	});
	frappe.posteingang=Object.assign(frappe.posteingang || {},{TaskSourceClient,attach_task_source:attach});
})();
