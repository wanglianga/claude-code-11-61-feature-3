<template>
  <span class="badge" :class="cls">{{ label }}</span>
</template>

<script setup>
import { computed } from 'vue'
import { STATUS_LABEL } from '../api'

const props = defineProps({ status: { type: String, default: '' } })

const MAP = {
  success: 'ok',
  succeeded: 'ok',
  approved: 'ok',
  published: 'info',
  running: 'info',
  pending: 'warn',
  needs_review: 'warn',
  blocked: 'danger',
  failed: 'danger',
  rejected: 'gray',
  superseded: 'gray',
  accepted: 'gray',
}

const cls = computed(() => MAP[props.status] || 'gray')
const label = computed(() => STATUS_LABEL[props.status] || props.status || '—')
</script>
