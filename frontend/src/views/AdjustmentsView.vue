<template>
  <div>
    <section class="panel">
      <div class="panel-head">
        <h2>调整台账</h2><span class="small muted">已发布账本的差额只追加调整，不改写原凭证</span>
        <div style="flex:1"></div>
        <select v-model="filterStatus" style="width:150px" @change="load">
          <option value="">全部状态</option>
          <option value="pending">待审批</option>
          <option value="approved">已批准</option>
          <option value="rejected">已驳回</option>
        </select>
        <button class="btn small" @click="load">刷新</button>
      </div>
      <table>
        <thead>
          <tr><th>调整编号</th><th>客户 / 活动</th><th>运行</th><th class="num">原金额</th><th class="num">新金额</th><th class="num">差额</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="a in filtered" :key="a.id">
            <td class="mono small">{{ a.code }}</td>
            <td><strong>{{ a.customer_code }}</strong><div class="small muted">{{ a.event_code }} · {{ a.event_date }}</div></td>
            <td class="small"><router-link :to="`/runs/${a.run_id}`">{{ a.run_code }}</router-link></td>
            <td class="num">{{ fmt.money(a.original_amount) }}</td>
            <td class="num">{{ fmt.money(a.new_amount) }}</td>
            <td class="num" :style="{ color: a.delta_amount < 0 ? 'var(--danger)' : 'var(--ok)' }">{{ fmt.money(a.delta_amount) }}</td>
            <td>
              <StatusBadge :status="a.status" />
              <div v-if="a.adjustment_voucher_id" class="small muted">凭证 #{{ a.adjustment_voucher_id }}</div>
            </td>
            <td style="white-space:nowrap">
              <template v-if="a.status === 'pending'">
                <button class="btn small primary" :disabled="!canApprove() || busy[a.id]" @click="decide(a, true)">批准</button>
                <button class="btn small" :disabled="!canApprove() || busy[a.id]" @click="decide(a, false)">驳回</button>
              </template>
              <router-link class="btn small" :to="`/diff?adjustment=${a.id}`">追溯来源</router-link>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!filtered.length" class="empty">暂无调整记录</div>
      <div v-if="message" class="panel-body"><div class="notice" :class="message.ok ? 'ok' : 'danger'">{{ message.text }}</div></div>
    </section>
    <p class="small muted" style="margin-top:10px">
      审批角色才能批准调整；批准后系统生成独立的 ADJV 调整凭证，引用原 STV 凭证号，原凭证金额与账本版本保持不变。
    </p>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, canApprove, fmt } from '../api'
import { periodStore } from '../periodStore'
import StatusBadge from '../components/StatusBadge.vue'

const rows = ref([])
const filterStatus = ref('')
const busy = ref({})
const message = ref(null)
const filtered = computed(() => (filterStatus.value ? rows.value.filter((r) => r.status === filterStatus.value) : rows.value))

async function load() {
  try {
    rows.value = await api('/adjustments', { params: { period_month: periodStore.period, status: filterStatus.value } })
  } catch (e) {
    message.value = { ok: false, text: e.message }
  }
}

async function decide(a, approve) {
  busy.value = { ...busy.value, [a.id]: true }
  try {
    const res = await api(`/adjustments/${a.id}/decision`, { method: 'POST', body: { approve, comment: '' } })
    message.value = {
      ok: true,
      text: approve
        ? `已批准 ${res.code}，生成调整凭证 #${res.adjustment_voucher_id}（差额 ${fmt.money(res.delta_amount)}）。`
        : `已驳回 ${res.code}。`,
    }
    await load()
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    busy.value = { ...busy.value, [a.id]: false }
  }
}

onMounted(load)
</script>
