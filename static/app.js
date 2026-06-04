
let chart;

function fmt(v, decimals = 1){
  if(v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${Number(v).toFixed(decimals)}`;
}

async function start(){
  const r = await fetch("/api/start", {method:"POST"});
  const data = await r.json();
  poll(data.job_id);
}

async function poll(jobId){
  const res = await fetch(`/api/status/${jobId}`);
  const job = await res.json();

  const pct = job.progress || 0;
  document.getElementById("loaderPct").textContent = `${pct}%`;
  document.getElementById("barFill").style.width = `${pct}%`;
  document.getElementById("loaderText").textContent = job.message || "Calculando...";

  if(job.status === "done"){
    render(job.result);
    document.getElementById("loader").classList.add("hidden");
    document.getElementById("dashboard").classList.remove("hidden");
    return;
  }

  if(job.status === "error"){
    document.getElementById("errorBox").textContent = `Error: ${job.error || job.message}`;
    document.getElementById("errorBox").classList.remove("hidden");
    return;
  }

  setTimeout(() => poll(jobId), 1200);
}

function render(data){
  document.getElementById("generatedAt").textContent = `Actualizado ${data.generated_at}`;
  document.getElementById("period").textContent = `${data.period.start} → ${data.period.end} GMT`;

  document.getElementById("totalCm").textContent = fmt(data.cards.total_cm);
  document.getElementById("astroCm").textContent = fmt(data.cards.astro_cm);
  document.getElementById("meteoCm").textContent = fmt(data.cards.meteo_cm);
  document.getElementById("galiboEntradaM").textContent = fmt(data.cards.galibo_entrada_m, 3);
  document.getElementById("galiboSalidaM").textContent = fmt(data.cards.galibo_salida_m, 3);

  document.getElementById("nextHigh").textContent = data.cards.next_high_time || "—";
  document.getElementById("nextHighVal").textContent = `${fmt(data.cards.next_high_cm)} cm`;
  document.getElementById("nextLow").textContent = data.cards.next_low_time || "—";
  document.getElementById("nextLowVal").textContent = `${fmt(data.cards.next_low_cm)} cm`;

  const tbody = document.getElementById("tableBody");
  tbody.innerHTML = "";
  for(const r of data.table){
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.fecha}</td>
      <td>${fmt(r.total)}</td>
      <td>${fmt(r.astro)}</td>
      <td>${fmt(r.meteo)}</td>
      <td>${fmt(r.galibo_entrada,3)}</td>
      <td>${fmt(r.galibo_salida,3)}</td>
      <td>${r.fuente}</td>
    `;
    tbody.appendChild(tr);
  }

  const ctx = document.getElementById("chart");
  if(chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: data.series.time,
      datasets: [
        {
          label: "Nivel total",
          data: data.series.total_cm,
          borderColor: "#c0392b",
          backgroundColor: "rgba(192,57,43,.08)",
          borderWidth: 3,
          pointRadius: 0,
          tension: .25,
          fill: true
        },
        {
          label: "Astronómica",
          data: data.series.astro_cm,
          borderColor: "#f2994a",
          borderWidth: 2,
          pointRadius: 0,
          tension: .25
        },
        {
          label: "Meteo / residuo",
          data: data.series.meteo_cm,
          borderColor: "#0d6c98",
          borderWidth: 2,
          pointRadius: 0,
          tension: .25
        },
        {
          label: "Mareógrafo PORTUS",
          data: alignMareo(data),
          borderColor: "#1e8449",
          borderWidth: 2,
          pointRadius: 0,
          tension: .25,
          spanGaps: true
        }
      ]
    },
    options: {
      responsive: true,
      interaction: {mode: "index", intersect: false},
      plugins: {
        legend: {position: "bottom"},
        tooltip: {callbacks:{label:(c)=>`${c.dataset.label}: ${fmt(c.raw)} cm`}}
      },
      scales: {
        y: {title:{display:true,text:"cm · Cero REDMAR"}, grid:{color:"rgba(16,32,51,.08)"}},
        x: {ticks:{maxRotation:45,minRotation:0}, grid:{display:false}}
      }
    }
  });
}

function alignMareo(data){
  const out = new Array(data.series.time.length).fill(null);
  const map = new Map();
  data.series.mareo_time.forEach((t, i) => map.set(t, data.series.mareo_cm[i]));
  data.series.time.forEach((t, i) => {
    if(map.has(t)) out[i] = map.get(t);
  });
  return out;
}

start();
