<template>
  <div>
    <section class="panel">
      <div class="panel-head">
        <h2>账本与凭证</h2><span class="small muted">{{ period }}</span>
        <div style="flex:1"></div>
        <label class="small muted" style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" v-model="includeSuperseded" style="width:auto" @change="load" /> 含历史版本
        </label>
        <button class="btn small" @click="load">刷新</button>
      </div>
      <table>
        <thead>
          <tr><th>客户 / 活动</th><th class="num">版本</th><th class="num">响应电量</th><th class="num">结算金额</th><th>状态</th><th>发布</th><th>付款</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="e in ledger" :key="e.id" :style="e.status === 'superseded' ? 'opacity:.6' : ''">
            <td><strong>{{ e.customer_code }}</strong><div class="small muted">{{ e.event_code }} · {{ e.event_date }}</div></td>
            <td class="num">v{{ e.version }}<div v-if="e.supersedes_id" class="small muted">替换 #{{ e.supersedes_id }}</div></td>
            <td class="num">{{ e.response_kwh }}</td>
            <td class="num">{{ fmt.money(e.amount) }}<div v-if="e.adjustment" class="small" :style="{color: e.adjustment.delta_amount < 0 ? 'var(--danger)' : 'var(--ok)'}">差异 {{ fmt.money(e.adjustment.delta_amount) }}</div></td>
            <td><StatusBadge :status="e.status" /></td>
            <td class="small">{{ fmt.dt(e.published_at) }}<div class="muted">{{ e.published_by }}</div></td>
            <td>
              <span v-if="e.paid" class="badge info">已付款</span>
              <button v-else class="btn small" :disabled="!canPlan() || e.status !== 'published' || paying[e.id]" @click="pay(e)">
                {{ paying[e.id] ? '…' : '登记付款' }}
              </button>
            </td>
            <td>
              <button class="btn small" @click="openLineage(e)">来源</button>
              <router-link v-if="e.adjustment" class="btn small" :to="`/diff?adjustment=${e.adjustment.id}`">追溯</router-link>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!ledger.length" class="empty">暂无账本，先发布一次成功运行</div>
      <div v-if="message" class="panel-body"><div class="notice" :class="message.ok ? 'ok' : 'danger'">{{ message.text }}</div></div>
    </section>

    <section class="panel" style="margin-top:14px">
      <div class="panel-head"><h2>凭证</h2><div style="flex:1"></div><span class="small muted">结算凭证 STV 与调整凭证 ADJV 分离，原凭证永不改写</span></div>
      <table>
        <thead><tr><th>凭证号</th><th>类型</th><th class="num">金额</th><th>引用</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="v in vouchers" :key="v.id">
            <td class="mono">{{ v.voucher_no }}</td>
            <td><span class="badge" :class="v.kind === 'adjustment' ? 'warn' : 'info'">{{ v.kind === 'adjustment' ? '调整' : '结算' }}</span></td>
            <td class="num">{{ fmt.money(v.amount) }}</td>
            <td class="small mono">{{ v.reference }}</td>
            <td class="small">{{ fmt.dt(v.paid_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!vouchers.length" class="empty">暂无凭证</div>
    </section>

    <section v-if="lineage" class="panel" style="margin-top:14px">
      <div class="panel-head"><h2>来源血缘：{{ lineage.customer?.code }} × {{ lineage.event?.code }}</h2><div style="flex:1"></div><button class="btn small" @click="lineage = null">收起</button></div>
      <div class="panel-body">
        <div class="flow">
          <template v-for="(n, idx) in lineage.nodes" :key="n.id">
            <div class="node" :class="n.status">
              <div style="display:flex;justify-content:space-between"><strong>{{ NODE_LABEL[n.node_type] }}</strong><StatusBadge :status="n.status" /></div>
              <div class="small muted">#{{ n.id }}</div>
              <div class="small" style="margin-top:5px">
                <span v-for="s in n.sources" :key="s.id" class="badge gray" style="margin:2px 3px 0 0">{{ s.source_type }} {{ s.source_type === 'batch' ? `#${s.ref_id} v${s.meta.version}` : `#${s.ref_id}` }}</span>
              </div>
            </div>
            <div v-if="idx < lineage.nodes.length - 1" class="arrow">→</div>
          </template>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, canPlan, fmt, NODE_LABEL } from '../api'
import { periodStore } from '../periodStore'
import StatusBadge from '../components/StatusBadge.vue'

const ledger = ref([])
const vouchers = ref([])
const lineage = ref(null)
const includeSuperseded = ref(true)
const paying = ref({})
const message = ref(null)
const period = computed(() => periodStore.period)

async function load() {
  try {
    const [l, v] = await Promise.all([
      api('/ledger', { params: { period_month: period.value, include_superseded: includeSuperseded.value } }),
      api('/vouchers'),
    ])
    ledger.value = l.filter((e) => includeSuperseded.value || e.status === 'published')
    vouchers.value = v
  } catch (e) {
    message.value = { ok: false, text: e.message }
  }
}

async function pay(entry) {
  paying.value = { ...paying.value, [entry.id]: true }
  try {
    const v = await api(`/ledger/${entry.id}/payment`, {
      method: 'POST',
      body: { reference: `PAY-${entry.id}-${Date.now()}` },
    })
    message.value = { ok: true, text: `已生成结算凭证 ${v.voucher_no}（${fmt.money(v.amount)}）。若之后签约或规则修订，差额将走调整凭证而非改写本凭证。` }
    await load()
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    paying.value = { ...paying.value, [entry.id]: false }
  }
}

async function openLineage(entry) {
  try {
    lineage.value = await api(`/lineage/${entry.run_id}/${entry.customer_id}/${entry.event_id}`)
  } catch (e) {
    message.value = { ok: false, text: e.message }
  }
}

onMounted(load)
</script>
