use serde::Serialize;
use std::collections::HashSet;
use std::fs;
use std::path::Path;

const BOOT_MILESTONE_PATHS: &[&str] = &[
    "/run/smolvm/milestones.jsonl",
    "/run/smolvm/boot-milestones.jsonl",
    "/var/log/smolvm-boot.log",
    "/var/log/boot.log",
];

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BootMilestone {
    pub stage: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub epoch_s: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub uptime_s: Option<f64>,
    pub raw: String,
}

#[derive(Debug, Serialize)]
pub struct BootMilestonesResponse {
    pub ok: bool,
    pub milestones: Vec<BootMilestone>,
    pub sources: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

pub fn read_boot_milestones() -> BootMilestonesResponse {
    let mut milestones = Vec::new();
    let mut sources = Vec::new();
    for path in BOOT_MILESTONE_PATHS {
        let path_obj = Path::new(path);
        if !path_obj.exists() {
            continue;
        }
        match fs::read_to_string(path_obj) {
            Ok(content) => {
                sources.push((*path).to_string());
                milestones.extend(parse_boot_milestones(&content));
            }
            Err(error) => {
                return BootMilestonesResponse {
                    ok: false,
                    milestones,
                    sources,
                    error: Some(format!("cannot read boot milestones: {error}")),
                };
            }
        }
    }
    let milestones = dedupe_boot_milestones(milestones);
    BootMilestonesResponse {
        ok: true,
        milestones,
        sources,
        error: None,
    }
}

fn parse_boot_milestones(content: &str) -> Vec<BootMilestone> {
    content
        .lines()
        .filter_map(parse_boot_milestone_line)
        .collect()
}

fn parse_boot_milestone_line(line: &str) -> Option<BootMilestone> {
    if let Some(json) = parse_json_milestone(line) {
        return Some(json);
    }
    parse_smolvm_ts_line(line)
}

fn parse_json_milestone(line: &str) -> Option<BootMilestone> {
    let value = serde_json::from_str::<serde_json::Value>(line).ok()?;
    let stage = value.get("stage")?.as_str()?.to_string();
    Some(BootMilestone {
        stage,
        epoch_s: value.get("epoch_s").and_then(serde_json::Value::as_f64),
        uptime_s: value.get("uptime_s").and_then(serde_json::Value::as_f64),
        raw: line.to_string(),
    })
}

fn parse_smolvm_ts_line(line: &str) -> Option<BootMilestone> {
    let marker = "SMOLVM_TS ";
    let start = line.find(marker)? + marker.len();
    let fields = &line[start..];
    let mut stage = None;
    let mut epoch_s = None;
    let mut uptime_s = None;
    for field in fields.split_whitespace() {
        let Some((key, value)) = field.split_once('=') else {
            continue;
        };
        match key {
            "stage" => stage = Some(value.to_string()),
            "epoch_s" => epoch_s = value.parse::<f64>().ok(),
            "uptime_s" => uptime_s = value.parse::<f64>().ok(),
            _ => {}
        }
    }
    Some(BootMilestone {
        stage: stage?,
        epoch_s,
        uptime_s,
        raw: line.to_string(),
    })
}

fn dedupe_boot_milestones(milestones: Vec<BootMilestone>) -> Vec<BootMilestone> {
    let mut seen = HashSet::new();
    let mut deduped = Vec::new();
    for milestone in milestones {
        let key = (
            milestone.stage.clone(),
            milestone.epoch_s.map(f64::to_bits),
            milestone.uptime_s.map(f64::to_bits),
        );
        if seen.insert(key) {
            deduped.push(milestone);
        }
    }
    deduped
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_console_and_json_boot_milestones() {
        let content = "\
SMOLVM_TS stage=init-start epoch_s=1781280000 uptime_s=0.10
{\"stage\":\"guest-agent-started\",\"epoch_s\":1781280001,\"uptime_s\":0.24}
unrelated line
";
        let milestones = parse_boot_milestones(content);
        assert_eq!(milestones.len(), 2);
        assert_eq!(milestones[0].stage, "init-start");
        assert_eq!(milestones[0].epoch_s, Some(1781280000.0));
        assert_eq!(milestones[0].uptime_s, Some(0.10));
        assert_eq!(milestones[1].stage, "guest-agent-started");
        assert_eq!(milestones[1].uptime_s, Some(0.24));
    }

    #[test]
    fn dedupes_same_stage_and_timestamp_keeping_first_source() {
        let first = BootMilestone {
            stage: "guest-agent-started".to_string(),
            epoch_s: Some(1781280001.0),
            uptime_s: Some(0.24),
            raw: "canonical".to_string(),
        };
        let duplicate = BootMilestone {
            raw: "duplicate".to_string(),
            ..first.clone()
        };
        let later_same_stage = BootMilestone {
            stage: "guest-agent-started".to_string(),
            epoch_s: Some(1781280002.0),
            uptime_s: Some(1.24),
            raw: "later".to_string(),
        };

        let milestones =
            dedupe_boot_milestones(vec![first.clone(), duplicate, later_same_stage.clone()]);

        assert_eq!(milestones, vec![first, later_same_stage]);
    }
}
