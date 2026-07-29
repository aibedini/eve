//! Canonical JSON for control-plane signatures (EVE_API_CONTRACT.md §1):
//! UTF-8, keys sorted recursively, no whitespace, `ensure_ascii=False`,
//! floats via shortest round-trip — i.e. the exact output of Python's
//! `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
//!
//! serde_json's number formatting (ryu, shortest round-trip, `1.0` printed as
//! `1.0`) matches Python's `repr` float formatting for all non-exponential
//! values, which is everything the eve config/job schemas produce. Integers
//! and floats that Python would print in exponent form (|x| >= 1e16 or
//! < 1e-4 with fractions) can differ in the exponent marker (`e+16` vs
//! `e16`); such values do not occur in the contract's schemas. We sort keys
//! explicitly rather than relying on serde_json's map representation.

use serde_json::Value;

/// Serialize `v` in the canonical form.
pub fn canonical_json(v: &Value) -> String {
    let mut s = String::new();
    write_canonical(v, &mut s);
    s
}

fn write_canonical(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_json_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(k, out);
                out.push(':');
                write_canonical(&map[*k], out);
            }
            out.push('}');
        }
    }
}

/// JSON string escaping identical to Python's json.dumps with
/// ensure_ascii=False: escape `"`, `\`, and C0 control characters (with the
/// usual short forms), pass everything else (including non-ASCII) through
/// as UTF-8.
fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Verify a signed control-plane object (config §2.2, job §2.3): the
/// `signature` field (base64 Ed25519) covers the whole object minus the
/// `signature` field, canonicalized.
pub fn verify_signed_object(
    object: &Value,
    cp_pubkey: &[u8; 32],
) -> Result<(), crate::ed25519::SignatureError> {
    let sig_b64 = object
        .get("signature")
        .and_then(Value::as_str)
        .ok_or(crate::ed25519::SignatureError::BadSignature)?;
    let sig = crate::ed25519::sig_from_b64(sig_b64)?;
    let mut stripped = object.clone();
    if let Value::Object(ref mut map) = stripped {
        map.remove("signature");
    }
    let msg = canonical_json(&stripped);
    crate::ed25519::verify(cp_pubkey, msg.as_bytes(), &sig)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Reference vector computed with Python 3.11:
    ///   json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    #[test]
    fn canonical_matches_python_nested_unicode() {
        let obj = json!({
            "z": [3, 1.5, {"b": "ünicode\u{2713}", "a": null}],
            "a": {"nested": {"x": true, "y": false}},
            "num": 1.33,
            "int": 42,
            "str": "IR-DE"
        });
        let expected = "{\"a\":{\"nested\":{\"x\":true,\"y\":false}},\"int\":42,\"num\":1.33,\"str\":\"IR-DE\",\"z\":[3,1.5,{\"a\":null,\"b\":\"ünicode\u{2713}\"}]}";
        assert_eq!(canonical_json(&obj), expected);
    }

    #[test]
    fn canonical_matches_python_config_object() {
        // Python: {"agent":{"name":"de-fra-1","role":"outside"},"config_version":7,"links":[]}
        let cfg = json!({
            "config_version": 7,
            "agent": {"name": "de-fra-1", "role": "outside"},
            "links": []
        });
        assert_eq!(
            canonical_json(&cfg),
            "{\"agent\":{\"name\":\"de-fra-1\",\"role\":\"outside\"},\"config_version\":7,\"links\":[]}"
        );
    }

    #[test]
    fn key_sorting_is_recursive_and_whitespace_free() {
        let obj = json!({"b": {"d": 1, "c": 2}, "a": [{"y": 1, "x": 2}]});
        assert_eq!(canonical_json(&obj), "{\"a\":[{\"x\":2,\"y\":1}],\"b\":{\"c\":2,\"d\":1}}");
    }

    #[test]
    fn string_escaping_matches_python() {
        let obj = json!({"s": "quote\" back\\slash\n tab\t ctrl\u{01} unicodeé"});
        // Python: json.dumps same input -> identical escapes, unicode raw.
        assert_eq!(
            canonical_json(&obj),
            "{\"s\":\"quote\\\" back\\\\slash\\n tab\\t ctrl\\u0001 unicodeé\"}"
        );
    }

    /// Python-generated signed config (cryptography module, Ed25519 seed =
    /// bytes 32..64); signature covers the object minus `signature`.
    #[test]
    fn verify_signed_config_from_python() {
        let sk = crate::ed25519::signing_key_from_seed(&core::array::from_fn(|i| (32 + i) as u8));
        let pk = sk.verifying_key().to_bytes();
        let signed = json!({
            "config_version": 7,
            "agent": {"name": "de-fra-1", "role": "outside"},
            "links": [],
            "signature": "rpplwJAOduZjhO4c+XdOa04Gv64DmQHzFPeoWxk63Bc6quO8drjfuRfyO5WxGPaA/xwwTynOTxIK3CtqSuASDA=="
        });
        verify_signed_object(&signed, &pk).unwrap();

        // Any tampering invalidates the signature.
        let mut tampered = signed.clone();
        tampered["config_version"] = json!(6);
        assert!(verify_signed_object(&tampered, &pk).is_err());

        // Missing signature field is an error, not a pass.
        let mut unsigned = signed.clone();
        unsigned.as_object_mut().unwrap().remove("signature");
        assert!(verify_signed_object(&unsigned, &pk).is_err());
    }
}
