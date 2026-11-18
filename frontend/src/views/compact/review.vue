<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>复核详情</h2>
        <p class="page-desc">核对小文件合并结果数据集的收益来源，并确认预计与实际的文件数量下降和容量下降。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/compact">返回合并列表</RouterLink>
        <RouterLink class="btn" to="/compact/workbench">合并收益核对工作台</RouterLink>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">结果数据集</span>
          <strong class="stat-value">{{ dataset['数据集编号'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">所属批次</span>
          <strong class="stat-value">{{ dataset['批次编号'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计文件数下降</span>
          <strong class="stat-value">{{ dataset['预计文件数下降'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计容量下降</span>
          <strong class="stat-value">{{ dataset['预计容量下降MB'] }}MB</strong>
        </article>
      </div>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">实际文件数下降</span>
          <strong class="stat-value">{{ dataset['实际文件数下降'] ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实际容量下降</span>
          <strong class="stat-value">
            {{ dataset['实际容量下降MB'] != null ? `${dataset['实际容量下降MB']}MB` : '—' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">候选分片数</span>
          <strong class="stat-value">{{ dataset['候选分片数'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">数据集状态</span>
          <strong class="stat-value">{{ dataset['状态'] }}</strong>
        </article>
      </div>

      <section class="panel">
        <header class="panel-head"><h3>复核清单</h3></header>
        <table class="data-table">
          <thead>
            <tr><th>目标表名</th><th>批次编号</th><th>收益类型</th><th>文件数下降</th><th>容量下降</th><th>复核状态</th></tr>
          </thead>
          <tbody>
            <tr v-if="reviewRow">
              <td>{{ reviewRow['目标表名'] }}</td>
              <td>{{ reviewRow['批次编号'] }}</td>
              <td>{{ reviewRow['收益类型'] }}</td>
              <td>{{ reviewRow['文件数下降'] }}</td>
              <td>{{ reviewRow['容量下降MB'] }}MB</td>
              <td>{{ reviewRow['复核状态'] }}</td>
            </tr>
            <tr v-else>
              <td colspan="6" class="empty-state">该批次还没有可复核的收益快照</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <header class="panel-head"><h3>收益来源</h3></header>
        <table class="data-table">
          <tbody>
            <tr>
              <th>计划收益</th>
              <td>文件数下降 {{ benefit['计划']?.['文件数下降'] ?? '—' }}，容量下降 {{ benefit['计划']?.['容量下降MB'] ?? '—' }}MB</td>
            </tr>
            <tr>
              <th>实际收益</th>
              <td>文件数下降 {{ benefit['实际']?.['文件数下降'] ?? '—' }}，容量下降 {{ benefit['实际']?.['容量下降MB'] ?? '—' }}MB</td>
            </tr>
            <tr>
              <th>统计报告</th>
              <td>
                <template v-if="benefit['统计报告']">
                  {{ benefit['统计报告']['报告编号'] }} · 文件总数 {{ benefit['统计报告']['文件总数'] }} ·
                  总容量 {{ benefit['统计报告']['总容量'] }} · {{ benefit['统计报告']['统计时间'] }}
                </template>
                <template v-else>该表还没有统计报告</template>
              </td>
            </tr>
            <tr>
              <th>候选分片</th>
              <td>{{ benefit['候选分片数'] }} 个，分区目录：{{ (dataset['分区目录'] || []).join('、') }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <header class="panel-head"><h3>版本依据</h3></header>
        <table class="data-table">
          <thead><tr><th>来源模块</th><th>版本依据</th><th>同步时间</th></tr></thead>
          <tbody>
            <tr v-for="item in versions" :key="String(item['id'])">
              <td>{{ item['来源模块'] }}</td>
              <td>{{ item['版本依据'] }}</td>
              <td>{{ item['同步时间'] }}</td>
            </tr>
            <tr v-if="!versions.length">
              <td colspan="3" class="empty-state">其它业务模块还没有同步版本依据</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { fetchJson } from '@/api/client'

type Dict = Record<string, any>

const route = useRoute()
const detail = ref<Dict | null>(null)
const errorMessage = ref('')

const dataset = computed<Dict>(() => detail.value?.dataset ?? {})
const benefit = computed<Dict>(() => detail.value?.['收益来源'] ?? {})
const reviewRow = computed<Dict | null>(() => detail.value?.['复核清单'] ?? null)
const versions = computed<Dict[]>(() => detail.value?.['版本依据'] ?? [])

onMounted(async () => {
  try {
    detail.value = await fetchJson<Dict>(`/api/compact/review/${route.params.datasetNo}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核详情读取失败'
  }
})
</script>
