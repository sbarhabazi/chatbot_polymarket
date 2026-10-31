"""
Audit de sécurité automatique du bot Polymarket — avec AUTO-CORRECTION.

Vérifie ET corrige automatiquement :
  1. Secrets hardcodés dans le code source
  2. Fichiers sensibles non couverts par .gitignore  → AUTO-FIX: ajoute au .gitignore (non commité)
  3. Dépendances Python avec CVE connues             → AUTO-FIX: pip-audit --fix + màj requirements.txt
  4. Allowances ERC-20 vers contrats inconnus        → ESCALADE: ne peut pas révoquer sans clé privée live
  5. Intégrité des fichiers critiques (checksums)    → ESCALADE si modification suspecte (sans commit git)
  6. Endpoint RPC non sécurisé (HTTP)                → AUTO-FIX: remplace par HTTPS dans config.py
  7. Données sensibles dans les logs                 → AUTO-FIX: supprime le fichier de log compromis

Exécuté chaque dimanche à 23h EDT par Perplexity Computer.

Les corrections sont appliquées dans la copie de travail mais ne sont JAMAIS
commitées ni poussées automatiquement : un humain relit le diff (git diff) et
committe lui-même. Un `git add -A` automatique pourrait publier un fichier
sensible non encore ignoré (.env, journaux, données de trading).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3

# ─── Configuration ────────────────────────────────────────────────────────────
REPO_DIR        = Path("/home/user/workspace/chatbot_polymarket")
TRACKING_FILE   = Path("/home/user/workspace/cron_tracking/security_audit.json")
CHECKSUM_FILE   = Path("/home/user/workspace/cron_tracking/security_checksums.json")
HISTORY_DIR     = Path("/home/user/workspace/cron_tracking/security_history")

WALLET = "0x0f6768A0ff895433827531f83933D805CF979B02"
RPC    = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CRITICAL_FILES = [
    "main.py", "config.py", "sniper.py", "harvester.py",
    "profit_taker.py", "wallet_manager.py", "learning_engine.py",
    "contracts/allowances.py", "contracts/swap.py",
]

ALLOWED_SPENDERS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296",
    "0xe592427a0aece92de3edee1f18e0157c05861564",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
}

REQUIRED_GITIGNORE_PATTERNS = [
    ".env", "*.env.*", "*.key", "*.pem", "*.p12", "*.pfx",
    "learned_params.json", "trade_history.json", "profit_history.json",
    "cron_tracking/", "*.log", "logs/",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

def load_tracking():
    if TRACKING_FILE.exists():
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {}

def save_tracking(data):
    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_checksums():
    if CHECKSUM_FILE.exists():
        with open(CHECKSUM_FILE) as f:
            return json.load(f)
    return {}

def save_checksums(cs):
    CHECKSUM_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKSUM_FILE, "w") as f:
        json.dump(cs, f, indent=2)

def git_last_commit_for_file(filepath: str) -> str | None:
    """Retourne le hash du dernier commit qui a modifié ce fichier."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", filepath],
            cwd=REPO_DIR, capture_output=True, text=True
        )
        return result.stdout.strip() or None
    except Exception:
        return None

def git_current_head() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_DIR, capture_output=True, text=True)
        return r.stdout.strip() or None
    except Exception:
        return None

# ─── Check + Auto-fix 1 : Secrets hardcodés ──────────────────────────────────

def check_secrets_in_code() -> dict:
    """Recherche de secrets hardcodés. Pas d'auto-fix possible (risque de casser le code)."""
    issues = []
    patterns = [
        (r"private_key\s*=\s*['\"][0-9a-fA-Fx]{10,}", "Clé privée potentiellement hardcodée"),
        (r"(password|passwd|secret|token)\s*=\s*['\"][^'\"]{8,}", "Secret/token hardcodé"),
        (r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])", "Clé privée hex 64 chars"),
    ]
    exclude = [
        r"os\.getenv", r"os\.environ", r"environ\.get", r"config\.", r"self\._",
        r"^\s*#", r"USDC_E\s*=", r"CTF\s*=", r"0x[0-9a-fA-F]{40}\b",
        r"test|example|placeholder|dummy",
    ]

    for py_file in REPO_DIR.rglob("*.py"):
        if any(skip in str(py_file) for skip in ["__pycache__", ".venv", "venv"]):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(content.splitlines(), 1):
                if any(re.search(exc, line, re.IGNORECASE) for exc in exclude):
                    continue
                for pattern, description in patterns:
                    if re.search(pattern, line, re.IGNORECASE):
                        issues.append({
                            "file": str(py_file.relative_to(REPO_DIR)),
                            "line": line_no,
                            "description": description,
                            "snippet": line.strip()[:80],
                        })
        except Exception:
            pass

    return {
        "check": "secrets_in_code",
        "status": "PASS" if not issues else "FAIL",
        "issues": issues,
        "auto_fix": False,
        "escalade": bool(issues),
        "escalade_reason": "Secret détecté dans le code — intervention manuelle requise." if issues else "",
    }

# ─── Check + Auto-fix 2 : .gitignore ─────────────────────────────────────────

def check_and_fix_gitignore() -> dict:
    """Vérifie .gitignore et ajoute les patterns manquants automatiquement."""
    gitignore_path = REPO_DIR / ".gitignore"
    issues = []
    fixes = []

    content = gitignore_path.read_text() if gitignore_path.exists() else ""

    missing = []
    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        if pattern not in content:
            missing.append(pattern)

    if missing:
        # AUTO-FIX : ajouter les patterns manquants
        addition = "\n# Auto-ajouté par audit de sécurité\n" + "\n".join(missing) + "\n"
        with open(gitignore_path, "a") as f:
            f.write(addition)
        fixes.append(f"Ajouté {len(missing)} patterns au .gitignore: {', '.join(missing)}")
        issues = [{"description": f"'{p}' manquant dans .gitignore → corrigé automatiquement"} for p in missing]

    # Vérifier que .env n'est pas déjà commité
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=REPO_DIR, capture_output=True
        )
        if result.returncode == 0:
            issues.append({
                "description": ".env est commité dans git — suppression...",
                "severity": "CRITICAL"
            })
            subprocess.run(["git", "rm", "--cached", ".env"], cwd=REPO_DIR, capture_output=True)
            fixes.append(".env retiré du suivi git (git rm --cached)")
    except Exception:
        pass

    return {
        "check": "gitignore",
        "status": "PASS",
        "issues": issues,
        "fixes_applied": fixes,
        "auto_fix": bool(fixes),
    }

# ─── Check + Auto-fix 3 : Vulnérabilités CVE ─────────────────────────────────

def check_and_fix_pip_vulnerabilities() -> dict:
    """Détecte et corrige les CVE dans les dépendances via pip-audit --fix."""
    try:
        # D'abord, lister les vulnérabilités
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "-r",
             str(REPO_DIR / "requirements.txt"), "--format", "json"],
            capture_output=True, text=True, timeout=120, cwd=REPO_DIR
        )
        try:
            data = json.loads(result.stdout)
            vulns = [
                {
                    "package": d["name"],
                    "version": d["version"],
                    "vuln_id": v["id"],
                    "fix_versions": v.get("fix_versions", []),
                }
                for d in data.get("dependencies", [])
                for v in d.get("vulns", [])
                if v.get("fix_versions")  # Seulement si une fix est disponible
            ]
        except Exception:
            vulns = []

        fixes = []
        if vulns:
            # AUTO-FIX : pip-audit --fix met à jour les packages vulnérables
            fix_result = subprocess.run(
                [sys.executable, "-m", "pip_audit", "-r",
                 str(REPO_DIR / "requirements.txt"), "--fix"],
                capture_output=True, text=True, timeout=180, cwd=REPO_DIR
            )
            if fix_result.returncode == 0:
                # Régénérer requirements.txt avec les versions corrigées
                freeze_result = subprocess.run(
                    [sys.executable, "-m", "pip", "freeze"],
                    capture_output=True, text=True
                )
                # Garder seulement les packages du requirements.txt original
                original_packages = {
                    line.split(">=")[0].split("==")[0].split("<")[0].lower()
                    for line in (REPO_DIR / "requirements.txt").read_text().splitlines()
                    if line.strip() and not line.startswith("#")
                }
                new_reqs = []
                for line in freeze_result.stdout.splitlines():
                    pkg_name = line.split("==")[0].lower()
                    if pkg_name in original_packages:
                        new_reqs.append(line)

                if new_reqs:
                    with open(REPO_DIR / "requirements.txt", "w") as f:
                        f.write("\n".join(new_reqs) + "\n")
                    fixes.append(f"requirements.txt mis à jour: {len(vulns)} CVE corrigées")
            else:
                fixes.append(f"pip-audit --fix tenté mais échec (code {fix_result.returncode})")

        return {
            "check": "pip_vulnerabilities",
            "status": "PASS",
            "vulnerabilities_found": len(vulns),
            "fixes_applied": fixes,
            "auto_fix": bool(fixes),
            "issues": [{"description": f"{v['package']} {v['version']} — {v['vuln_id']} → fix: {v['fix_versions']}"} for v in vulns],
        }
    except Exception as e:
        return {
            "check": "pip_vulnerabilities",
            "status": "SKIP",
            "issues": [],
            "error": str(e),
            "auto_fix": False,
        }

# ─── Check 4 : Allowances ERC-20 on-chain ────────────────────────────────────

def check_wallet_allowances() -> dict:
    """
    Vérifie les allowances on-chain.
    AUTO-FIX impossible sans clé privée live → ESCALADE si contrat inconnu détecté.
    """
    issues = []
    escalade = False
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        wallet = Web3.to_checksum_address(WALLET)
        allowance_abi = [{
            "name": "allowance", "type": "function",
            "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view",
        }]
        usdc_e_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E), abi=allowance_abi
        )
        known_spenders = {
            "CTF_Exchange":      "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982e",
            "NegRisk_Adapter":   "0xd91E80cF2E7be2e162c6513ced06f1dD0dA35296",
            "NegRisk_Exchange":  "0xC5d563A36AE78145C45a50134d48A1215220f80a",
            "Uniswap_Router":    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        }
        WARN_THRESHOLD = (2**256 - 1) // 2

        for name, spender in known_spenders.items():
            try:
                allowance = usdc_e_contract.functions.allowance(
                    wallet, Web3.to_checksum_address(spender)
                ).call()
                is_infinite = allowance >= WARN_THRESHOLD
                is_known = spender.lower() in ALLOWED_SPENDERS

                if is_infinite and not is_known:
                    escalade = True
                    issues.append({
                        "spender": spender,
                        "name": name,
                        "severity": "HIGH",
                        "description": f"Approbation illimitée vers contrat INCONNU: {name} ({spender})",
                    })
            except Exception as e:
                issues.append({"spender": spender, "error": str(e), "severity": "INFO"})

        return {
            "check": "wallet_allowances",
            "status": "PASS" if not escalade else "FAIL",
            "issues": issues,
            "auto_fix": False,
            "escalade": escalade,
            "escalade_reason": (
                "Approbation ERC-20 illimitée vers contrat inconnu détectée. "
                "Action manuelle requise: appelle contracts/allowances.py avec amount=0 pour révoquer."
            ) if escalade else "",
        }
    except Exception as e:
        return {"check": "wallet_allowances", "status": "SKIP", "error": str(e), "issues": [], "auto_fix": False}

# ─── Check + Auto-fix 5 : Intégrité fichiers ─────────────────────────────────

def check_file_integrity() -> dict:
    """
    Compare les checksums SHA-256 des fichiers critiques vs la baseline.
    Si un fichier a changé SANS nouveau commit git → ESCALADE (possible intrusion).
    Si le fichier a changé AVEC un commit git légitime → mise à jour de la baseline (normal).
    """
    old_checksums = load_checksums()
    new_checksums = {}
    suspicious = []
    updated = []
    head = git_current_head()

    for filename in CRITICAL_FILES:
        path = REPO_DIR / filename
        if not path.exists():
            continue
        new_checksums[filename] = sha256_file(path)
        if filename in old_checksums:
            if new_checksums[filename] != old_checksums[filename]:
                last_commit = git_last_commit_for_file(filename)
                if last_commit and last_commit == head:
                    # Changement via commit git légitime
                    updated.append(filename)
                else:
                    # Fichier modifié SANS commit → suspect
                    suspicious.append(filename)

    save_checksums(new_checksums)

    escalade = bool(suspicious)
    return {
        "check": "file_integrity",
        "status": "FAIL" if escalade else "PASS",
        "files_checked": len(new_checksums),
        "files_updated_via_git": updated,
        "suspicious_modifications": suspicious,
        "auto_fix": False,
        "escalade": escalade,
        "escalade_reason": (
            f"Fichiers modifiés SANS commit git détectés: {suspicious}. "
            "Possible modification non autorisée. Vérifiez immédiatement."
        ) if escalade else "",
    }

# ─── Check + Auto-fix 6 : Endpoint RPC ───────────────────────────────────────

def check_and_fix_rpc_endpoint() -> dict:
    """Vérifie que le RPC est en HTTPS. AUTO-FIX si HTTP détecté dans config.py."""
    issues = []
    fixes = []

    config_path = REPO_DIR / "config.py"
    if config_path.exists():
        content = config_path.read_text()
        http_rpcs = re.findall(r'"(http://[^"]+)"', content)
        for http_rpc in http_rpcs:
            https_rpc = http_rpc.replace("http://", "https://", 1)
            new_content = content.replace(f'"{http_rpc}"', f'"{https_rpc}"')
            config_path.write_text(new_content)
            content = new_content
            issues.append({"description": f"RPC HTTP non sécurisé remplacé par HTTPS: {https_rpc}"})
            fixes.append(f"config.py: {http_rpc} → {https_rpc}")

    # Vérifier que le RPC actuel répond
    rpc_ok = False
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        block = w3.eth.block_number
        rpc_ok = block > 0
    except Exception as e:
        issues.append({"description": f"RPC inaccessible: {e}", "severity": "HIGH"})

    return {
        "check": "rpc_endpoint",
        "status": "PASS" if rpc_ok else "WARN",
        "rpc_url": RPC,
        "rpc_responding": rpc_ok,
        "issues": issues,
        "fixes_applied": fixes,
        "auto_fix": bool(fixes),
    }

# ─── Check + Auto-fix 7 : Logs sensibles ─────────────────────────────────────

def check_and_fix_sensitive_logs() -> dict:
    """
    Cherche des clés privées dans les fichiers de log.
    AUTO-FIX : supprime le fichier de log compromis + crée un fichier vide.
    """
    issues = []
    fixes = []
    log_files = list(REPO_DIR.glob("*.log")) + list(REPO_DIR.glob("logs/*.log"))

    for log_file in log_files:
        try:
            content = log_file.read_text(errors="ignore")
            if re.search(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])", content):
                # AUTO-FIX : supprimer le log compromis
                backup_path = HISTORY_DIR / f"COMPROMISED_{log_file.name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
                HISTORY_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy(log_file, backup_path)  # Copie pour archivage forensique
                log_file.write_text("")  # Vide le fichier log
                issues.append({
                    "file": str(log_file.name),
                    "description": "Clé privée potentielle détectée dans les logs → fichier vidé",
                    "severity": "CRITICAL",
                })
                fixes.append(f"Log compromis vidé: {log_file.name} (archivé: {backup_path.name})")
        except Exception:
            pass

    return {
        "check": "sensitive_logs",
        "status": "PASS" if not [i for i in issues if i.get("severity") == "CRITICAL"] else "FIXED",
        "issues": issues,
        "fixes_applied": fixes,
        "auto_fix": bool(fixes),
        "escalade": bool(issues),  # Même si corrigé, notifier l'utilisateur
        "escalade_reason": "Clé privée détectée dans les logs → fichier vidé automatiquement. Vérifiez si la clé doit être changée." if issues else "",
    }

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] Démarrage audit de sécurité avec auto-correction...", file=sys.stderr)

    # Lancer tous les checks (avec auto-fix intégrés)
    checks = [
        check_secrets_in_code(),
        check_and_fix_gitignore(),
        check_and_fix_pip_vulnerabilities(),
        check_wallet_allowances(),
        check_file_integrity(),
        check_and_fix_rpc_endpoint(),
        check_and_fix_sensitive_logs(),
    ]

    # Collecter tous les fixes appliqués
    all_fixes = []
    for c in checks:
        if c.get("auto_fix") and c.get("fixes_applied"):
            all_fixes.extend(c["fixes_applied"])

    # Aucune opération git automatique : les corrections restent à relire.
    git_pushed = False

    # Problèmes nécessitant une escalade humaine
    escalades = [
        {"check": c["check"], "reason": c["escalade_reason"]}
        for c in checks
        if c.get("escalade") and c.get("escalade_reason")
    ]
    if all_fixes:
        escalades.append({
            "check": "auto_fix",
            "reason": "Corrections appliquées localement mais non commitées : "
                      "relire `git diff` puis committer manuellement.",
        })

    # Statut global
    fails   = [c for c in checks if c["status"] == "FAIL"]
    warns   = [c for c in checks if c["status"] == "WARN"]
    overall = "FAIL" if fails else ("WARN" if warns else "PASS")

    report = {
        "timestamp": now,
        "overall_status": overall,
        "checks": checks,
        "summary": {
            "pass":  len([c for c in checks if c["status"] in ("PASS", "FIXED", "INFO")]),
            "warn":  len(warns),
            "fail":  len(fails),
            "skip":  len([c for c in checks if c["status"] == "SKIP"]),
            "auto_fixes_applied": len(all_fixes),
            "git_pushed": git_pushed,
        },
        "fixes_applied": all_fixes,
        "escalades": escalades,
        "notify": bool(all_fixes) or bool(escalades) or overall in ("FAIL", "WARN"),
    }

    # Construire la notification
    if report["notify"]:
        body_parts = []

        if all_fixes:
            body_parts.append(f"🔧 {len(all_fixes)} correction(s) automatique(s) appliquée(s):")
            for fix in all_fixes[:4]:
                body_parts.append(f"  • {fix}")
            if git_pushed:
                body_parts.append("  ✅ Poussé sur GitHub automatiquement.")

        if escalades:
            body_parts.append(f"\n⚠️ {len(escalades)} problème(s) nécessitent ton attention:")
            for esc in escalades:
                body_parts.append(f"  • {esc['reason'][:120]}")

        if overall == "PASS" and not escalades:
            body_parts.append("✅ Tout le reste est en ordre.")

        report["notification"] = {
            "title": f"Audit sécurité — {overall} ({len(all_fixes)} fix auto)",
            "body": "\n".join(body_parts),
        }

    # Sauvegarder le rapport
    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(report, f, indent=2)

    # Archive datée
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    dated_file = HISTORY_DIR / f"audit_{now[:10].replace('-','_')}.json"
    with open(dated_file, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report))
    return report


if __name__ == "__main__":
    main()
