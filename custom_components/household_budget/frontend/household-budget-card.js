class HouseholdBudgetCard extends HTMLElement {
  setConfig(config) {
    const modes = ["entry", "categories", "recent"];
    if (!modes.includes(config.mode)) throw new Error("mode must be entry, categories, or recent");
    this.config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  set hass(value) {
    const previousUpdate = this._hass?.states?.["sensor.household_budget_last_update"]?.state;
    this._hass = value;
    if (!this._loaded) this._load();
    else if (this.config?.mode !== "entry" && previousUpdate !== value.states?.["sensor.household_budget_last_update"]?.state) {
      this._loaded = false;
      this._load();
    }
  }

  getCardSize() { return this.config?.mode === "entry" ? 6 : 4; }

  disconnectedCallback() { this._clearPreview(); }

  async _api(path, method = "GET", body) {
    const options = body === undefined ? undefined : body;
    return this._hass.callApi(method, `household_budget/${path}`, options);
  }

  async _load() {
    if (!this._hass || !this.config || this._loading) return;
    this._loading = true;
    try {
      if (this.config.mode === "entry") this.data = await this._api("config");
      if (this.config.mode === "categories") this.data = await this._api("summary");
      if (this.config.mode === "recent") {
        this.data = await this._api(`recent?limit=${Number(this.config.limit || 10)}`);
      }
      this.error = "";
    } catch (error) {
      this.error = error.message || String(error);
    } finally {
      this._loaded = true;
      this._loading = false;
      this._render();
    }
  }

  _styles() {
    return `<style>
      :host { display:block; color:var(--primary-text-color); }
      ha-card { padding:16px; overflow:hidden; }
      h2 { margin:0 0 12px; font-size:1.25rem; line-height:1.3; }
      p { line-height:1.5; }
      .actions,.grid { display:grid; gap:12px; }
      .grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
      button,input,select { box-sizing:border-box; min-height:48px; width:100%; font:inherit; }
      button { border:1px solid var(--divider-color); border-radius:var(--ha-card-border-radius,12px); padding:0 16px; background:var(--secondary-background-color); color:var(--primary-text-color); cursor:pointer; white-space:nowrap; }
      button.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:var(--primary-color); font-weight:600; }
      button:disabled { opacity:.55; cursor:not-allowed; }
      button:focus-visible,input:focus-visible,select:focus-visible { outline:3px solid var(--primary-color); outline-offset:2px; }
      label { display:grid; gap:6px; font-weight:500; }
      input,select { border:1px solid var(--input-idle-line-color,var(--divider-color)); border-radius:8px; padding:10px 12px; color:var(--primary-text-color); background:var(--card-background-color); }
      .full { grid-column:1/-1; }
      .status,.error { margin:12px 0 0; padding:12px; border-radius:8px; background:var(--secondary-background-color); }
      .error { border-inline-start:4px solid var(--error-color,#db4437); }
      .row { padding:12px 0; border-bottom:1px solid var(--divider-color); }
      .row:last-child { border-bottom:0; }
      .row-head { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
      .muted { color:var(--secondary-text-color); font-size:.9rem; }
      progress { width:100%; height:12px; margin-top:8px; accent-color:var(--primary-color); }
      .receipt-preview { display:block; max-width:100%; max-height:320px; margin:0 auto 16px; border-radius:8px; border:1px solid var(--divider-color); object-fit:contain; }
      .expense { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px 12px; align-items:center; }
      .expense button { width:auto; min-width:88px; }
      .hidden { display:none; }
      @media (max-width:420px) { .grid { grid-template-columns:1fr; } .full { grid-column:auto; } ha-card { padding:14px; } }
      @media (prefers-reduced-motion:reduce) { * { scroll-behavior:auto!important; transition:none!important; } }
    </style>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    if (!this._loaded) {
      this.shadowRoot.innerHTML = `${this._styles()}<ha-card><p role="status">Loading budget…</p></ha-card>`;
      return;
    }
    let content = "";
    if (this.config.mode === "entry") content = this._entryView();
    if (this.config.mode === "categories") content = this._categoryView();
    if (this.config.mode === "recent") content = this._recentView();
    this.shadowRoot.innerHTML = `${this._styles()}<ha-card>${content}<div class="${this.error ? "error" : "hidden"}" role="alert">${this._escape(this.error)}</div><div id="live" class="status ${this.status ? "" : "hidden"}" aria-live="polite">${this._escape(this.status || "")}</div></ha-card>`;
    this._bind();
  }

  _entryView() {
    if (!this.formOpen) {
      return `<h2>Add an expense</h2><div class="actions"><button id="scan" class="primary"><ha-icon icon="mdi:camera-outline"></ha-icon> Scan a receipt</button><input id="receipt" class="hidden" type="file" accept="image/jpeg,image/png,application/pdf" capture="environment"><button id="manual"><ha-icon icon="mdi:pencil-outline"></ha-icon> Enter manually</button></div>`;
    }
    const draft = this.draft || {};
    if (!this.lastMember) this.lastMember = localStorage.getItem("household-budget-last-member") || "";
    const today = new Date().toISOString().slice(0,10);
    const categories = (this.data?.categories || []).filter((x) => x.accepts_expenses).map((x) => ({ value:x.parent ? `${x.parent}/${x.name}` : x.name, label:x.parent ? `${x.parent} / ${x.name}` : x.name }));
    const options = (values, selected) => values.map((x) => `<option value="${this._escape(x.value ?? x)}" ${(x.value ?? x) === selected ? "selected" : ""}>${this._escape(x.label ?? x)}</option>`).join("");
    const preview = this.previewUrl && this.previewType?.startsWith("image/") ? `<img class="receipt-preview" src="${this._escape(this.previewUrl)}" alt="Uploaded receipt preview">` : (this.previewName ? `<p class="status">Uploaded receipt: ${this._escape(this.previewName)}</p>` : "");
    return `<h2>${this.draftId ? "Review scanned receipt" : "Enter expense"}</h2>${preview}<p class="muted">Review every value before saving. OpenAI output is never posted automatically.</p><form id="expense-form" class="grid">
      <label>Amount<input id="amount" inputmode="decimal" required placeholder="0.00" value="${this._escape(draft.total || "")}"></label>
      <label>Date<input id="date" type="date" required value="${this._escape(draft.occurred_on || today)}"></label>
      <label>Household member<select id="member" required>${options(this.data?.members || [], this.lastMember || "")}</select></label>
      <label>Category<select id="category" required><option value="">Choose a category</option>${options(categories, draft.suggested_category || "")}</select></label>
      <label class="full">Merchant or description<input id="description" maxlength="1000" value="${this._escape(draft.merchant || "")}"></label>
      <label id="large-wrap" class="full ${Number(draft.total || 0) > 500 ? "" : "hidden"}"><span><input id="large" type="checkbox" style="width:24px;min-height:24px;vertical-align:middle"> I reviewed and confirm this expense over $500</span></label>
      <button type="button" id="cancel">Cancel</button><button type="submit" class="primary" id="save">${this.draftId ? "Confirm expense" : "Add expense"}</button>
    </form>`;
  }

  _categoryView() {
    const rows = (this.data?.category_rows || []).filter((x) => !x.parent);
    return `<h2>Categories</h2>${rows.map((row) => { const budget=Number(row.budget_cents||0); const spent=Number(row.spent_cents||0); const percent=budget ? Math.round(spent*100/budget) : 0; return `<div class="row"><div class="row-head"><strong>${this._escape(row.name)}</strong><span>$${(spent/100).toFixed(2)} of $${(budget/100).toFixed(2)}</span></div><div class="muted">${percent}% used · $${((budget-spent)/100).toFixed(2)} remaining</div><progress max="100" value="${Math.min(percent,100)}" aria-label="${this._escape(row.name)} ${percent}% used"></progress></div>`; }).join("") || "<p>No category data is available.</p>"}`;
  }

  _recentView() {
    return `<h2>Recent expenses</h2>${(this.data?.expenses || []).map((row) => `<div class="row expense"><div><strong>${this._escape(row.description || row.category)}</strong><div class="muted">${this._escape(row.member)} · ${this._escape(row.parent_category ? `${row.parent_category}/${row.category}` : row.category)} · ${new Date(row.occurred_at).toLocaleDateString()}</div></div><div><strong>$${(Number(row.amount_cents)/100).toFixed(2)}</strong><button class="undo" data-entry="${Number(row.id)}" data-member="${this._escape(row.member)}">Undo</button></div></div>`).join("") || "<p>No recent expenses.</p>"}`;
  }

  _bind() {
    const q = (selector) => this.shadowRoot.querySelector(selector);
    q("#scan")?.addEventListener("click", () => q("#receipt").click());
    q("#receipt")?.addEventListener("change", (event) => this._scan(event.target.files[0]));
    q("#manual")?.addEventListener("click", () => { this.pendingRequestId=null; this.formOpen=true; this._render(); q("#amount")?.focus(); });
    q("#cancel")?.addEventListener("click", () => { this.pendingRequestId=null; this._clearPreview(); this.formOpen=false; this.draft=null; this.draftId=null; this.error=""; this._render(); });
    q("#amount")?.addEventListener("input", (event) => q("#large-wrap").classList.toggle("hidden", Number(event.target.value) <= 500));
    q("#expense-form")?.addEventListener("input", () => { this.pendingRequestId=null; });
    q("#expense-form")?.addEventListener("change", () => { this.pendingRequestId=null; });
    q("#expense-form")?.addEventListener("submit", (event) => { event.preventDefault(); this._save(); });
    this.shadowRoot.querySelectorAll(".undo").forEach((button) => button.addEventListener("click", () => this._undo(button.dataset.member, Number(button.dataset.entry))));
  }

  async _scan(file) {
    if (!file) return;
    this.pendingRequestId=null;
    this._clearPreview(); this.previewUrl=URL.createObjectURL(file); this.previewType=file.type; this.previewName=file.name;
    this.status="Uploading and analyzing receipt…"; this.error=""; this._render();
    try {
      const response = await fetch("/api/household_budget/receipt", { method:"POST", headers:{ "Authorization":`Bearer ${this._hass.auth.data.access_token}`, "Content-Type":file.type, "X-Receipt-Filename":encodeURIComponent(file.name) }, body:file });
      const result = await response.json();
      if (!response.ok) {
        if (result.draft) {
          this.draft=result.draft; this.draftId=result.draft.id; this.formOpen=true;
          this.status="Automatic analysis failed. Enter the receipt values manually, then review and confirm.";
        }
        throw new Error(result.error || "Receipt analysis failed");
      }
      this.draft=result.draft; this.draftId=result.draft.id; this.formOpen=true; this.status=result.duplicate ? "This receipt was uploaded before. Review the existing draft carefully." : "Receipt analyzed. Review every field before confirming.";
    } catch (error) { this.error=error.message || String(error); if (!this.draftId) this.status=""; }
    this._render();
  }

  async _save() {
    const q=(s)=>this.shadowRoot.querySelector(s); const amount=q("#amount").value.trim();
    if (Number(amount)>500 && !q("#large")?.checked) { this.error="Review and acknowledge the expense over $500."; this._render(); return; }
    const member=q("#member").value; const category=q("#category").value; const description=q("#description").value.trim(); const occurredOn=q("#date").value;
    this.pendingRequestId ||= crypto.randomUUID();
    const payload={ request_id:this.pendingRequestId, amount, member, category, description, occurred_on:occurredOn, confirm_large_expense:Number(amount)>500 };
    this.draft={...(this.draft||{}),total:amount,merchant:description,occurred_on:occurredOn,suggested_category:category};
    this.lastMember=member; localStorage.setItem("household-budget-last-member",member); this.status="Saving expense…"; this.error=""; this._render();
    try { await this._api(this.draftId ? `receipt/${this.draftId}/confirm` : "expense", "POST", payload); this.status="Expense added successfully."; this.pendingRequestId=null; this._clearPreview(); this.formOpen=false; this.draft=null; this.draftId=null; }
    catch(error){ this.error=error.message || String(error); this.status=""; }
    this._render();
  }

  async _undo(member, entryId) {
    if (!confirm(`Undo this expense for ${member}? The audit history will be retained.`)) return;
    try { await this._api("undo","POST",{request_id:crypto.randomUUID(),member,entry_id:entryId}); this.status="Expense reversed successfully."; this._loaded=false; await this._load(); }
    catch(error){ this.error=error.message || String(error); this._render(); }
  }

  _clearPreview() { if (this.previewUrl) URL.revokeObjectURL(this.previewUrl); this.previewUrl=null; this.previewType=null; this.previewName=null; }

  _escape(value) { const div=document.createElement("div"); div.textContent=String(value ?? ""); return div.innerHTML; }
}

customElements.define("household-budget-card", HouseholdBudgetCard);
window.customCards = window.customCards || [];
window.customCards.push({ type:"household-budget-card", name:"Household Budget", description:"Phone-first expense entry, receipt review, categories, and recent expenses." });
