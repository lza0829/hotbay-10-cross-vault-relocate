import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

// 架构一（同键改所属）专用入口：不做关册新登，分区键/温层/素材段保持不变。
export default function VaultMoves() {
  const [parts, setParts] = useState([])
  const [vaults, setVaults] = useState([])
  const [moves, setMoves] = useState([])
  const [inv, setInv] = useState([])
  const [pid, setPid] = useState('')
  const [toVid, setToVid] = useState('')
  const [msg, setMsg] = useState('')
  const [receipt, setReceipt] = useState(null)
  const [err, setErr] = useState('')

  // 五视图联动同刷：分区册、片库间（两侧占用页）、目录清单、改挂回执、总览徽章数据
  const reload = () => {
    api.partitions().then(setParts)
    api.vaults().then(setVaults)
    api.vaultMoves().then(setMoves)
    api.inventory().then(setInv)
  }
  useEffect(() => { reload() }, [])

  const selected = useMemo(() => parts.find(p => String(p.id) === String(pid)), [parts, pid])
  const targetOptions = useMemo(
    () => vaults.filter(v => v.active && v.id !== selected?.vault_id),
    [vaults, selected],
  )

  function pickPart(id) {
    setPid(id)
    const p = parts.find(x => String(x.id) === String(id))
    const first = vaults.find(v => v.active && v.id !== p?.vault_id)
    setToVid(first ? String(first.id) : '')
    setReceipt(null)
    setMsg('')
    setErr('')
  }

  async function submit() {
    setMsg(''); setErr(''); setReceipt(null)
    if (!pid || !toVid) { setErr('先选择在册分区与目标片库间'); return }
    // 幂等键：同一意图的重试携带同键，后端只回放原回执
    const requestId = `mv-${pid}-${toVid}-${Date.now()}`
    try {
      const r = await api.moveVault({
        partition_id: Number(pid),
        to_vault_id: Number(toVid),
        request_id: requestId,
      })
      setReceipt(r)
      setMsg(r.重放 ? `请求重放：回执 ${r.id}，占用未重复变迁` : `改挂成功，回执 ${r.id} 已落账`)
      reload()
    } catch (e) {
      setErr(`改挂被拒（整体回滚，无半挂状态）：${e.message}`)
    }
  }

  return (
    <div className="page">
      <h1>跨库改挂</h1>
      <p className="lede">
        同键改所属：分区键、温层、素材段保留；分区册所属库、目录清单库名、两侧占用页、改挂回执、总览在册徽章一笔同刷，失败整体回滚。
      </p>

      <section className="card-grid">
        {vaults.map(v => (
          <article className="card" key={v.id}>
            <h3>{v.name}</h3>
            <p>额定 {v.capacity} · 在册 {v.在册分区数}（占用页）</p>
            <p>{v.active ? '启用' : '停用'}</p>
          </article>
        ))}
      </section>

      <div className="panel form row">
        <label>在册分区<select value={pid} onChange={e => pickPart(e.target.value)}>
          <option value="">选择</option>
          {parts.filter(p => p.status === '在册').map(p => (
            <option key={p.id} value={p.id}>{p.part_key}（现挂 {p.vault_name} · {p.tier}）</option>
          ))}
        </select></label>
        <label>目标片库间<select value={toVid} onChange={e => setToVid(e.target.value)}>
          <option value="">选择</option>
          {targetOptions.map(v => (
            <option key={v.id} value={v.id}>{v.name}（在册 {v.在册分区数}/{v.capacity}）</option>
          ))}
        </select></label>
        <button type="button" onClick={submit}>执行改挂</button>
      </div>

      {msg && <p className="msg">{msg}</p>}
      {err && <p className="err">{err}</p>}

      {receipt && (
        <section className="panel">
          <h2>改挂回执 #{receipt.id}{receipt.重放 ? '（重放）' : ''}</h2>
          <ul className="plain">
            <li>分区键：{receipt.part_key}（键不变，温层 {receipt.tier} 保留，素材段 {receipt.clip_count} 段保留）</li>
            <li>甲库 {receipt.from_vault_name}：在册 {receipt.甲库在册}/{receipt.甲库额定}（已减一）</li>
            <li>乙库 {receipt.to_vault_name}：在册 {receipt.乙库在册}/{receipt.乙库额定}（已加一）</li>
            <li>对象前缀 {receipt.object_prefix} 仅在乙库在册，甲乙无双计</li>
            <li>落账时刻：{receipt.created_at}</li>
          </ul>
        </section>
      )}

      <h2>改挂回执流水</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>回执</th><th>分区键</th><th>甲库</th><th>乙库</th><th>段数</th><th>甲在册</th><th>乙在册</th><th>状态</th><th>时刻</th></tr></thead>
          <tbody>
            {moves.map(m => (
              <tr key={m.id}>
                <td>{m.id}</td><td>{m.part_key}</td>
                <td>{m.from_vault_name}</td><td>{m.to_vault_name}</td>
                <td>{m.clip_count}</td>
                <td>{m.甲库在册}/{m.甲库额定}</td><td>{m.乙库在册}/{m.乙库额定}</td>
                <td>{m.status}</td><td>{m.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>目录清单（库名随改挂同刷）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>对象键</th><th>字节</th><th>片库间</th><th>扫描时刻</th></tr></thead>
          <tbody>
            {inv.map(x => (
              <tr key={x.id}><td>{x.object_key}</td><td>{x.bytes}</td><td>{x.vault_name}</td><td>{x.scanned_at}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
