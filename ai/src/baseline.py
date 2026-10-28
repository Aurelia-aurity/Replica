"""Offline, Slurm GPU-only Qwen baseline. No API or automatic downloads."""
import argparse
import json
import os
from pathlib import Path
import sys

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"
MESSAGES = {
    "CONFIG": "설정·질문·토큰 제한을 확인하세요.",
    "MODEL_PATH": "다운로드된 로컬 모델 디렉터리를 지정하세요.",
    "ENVIRONMENT": "Python 3.12 가상환경에서 실행하세요.",
    "ALLOCATION": "Slurm으로 GPU를 할당받은 작업 셸에서 실행하세요.",
    "DEPENDENCY": "GPU 가상환경의 오프라인 패키지 설치를 확인하세요.",
    "CUDA": "할당된 CUDA GPU에 접근할 수 없습니다.",
    "BF16": "이 GPU는 네이티브 BF16 추론을 지원하지 않습니다.",
    "LOAD": "로컬 모델·토크나이저 파일과 설치 버전을 확인하세요.",
    "PLACEMENT": "CPU 또는 offload 배치가 감지되었습니다.",
    "INPUT": "chat template 또는 입력 길이를 확인하세요.",
    "GENERATE": "GPU 추론에 실패했습니다. 할당·메모리·입력을 확인하세요.",
}


class BaselineError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes raw arguments in stderr.
        raise BaselineError("CONFIG") from None


def read_settings(config_path, prompt, model_path, max_new_tokens=None):
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        system = config["system_prompt"]
        limit = config["max_new_tokens"] if max_new_tokens is None else max_new_tokens
        if not isinstance(system, str) or not system.strip():
            raise ValueError
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
            raise ValueError
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise BaselineError("CONFIG") from None
    if not model_path or not isinstance(model_path, str):
        raise BaselineError("MODEL_PATH")
    try:
        path = Path(model_path).expanduser().resolve(strict=True)
        if not path.is_dir() or not (path / "config.json").is_file():
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise BaselineError("MODEL_PATH") from None
    return {"model_path": str(path), "system_prompt": system, "prompt": prompt,
            "max_new_tokens": limit}


def check_environment():
    if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix:
        raise BaselineError("ENVIRONMENT")
    if not os.environ.get("SLURM_JOB_ID", "").strip():
        raise BaselineError("ALLOCATION")


def infer(settings):
    check_environment()
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig
    except Exception:
        raise BaselineError("DEPENDENCY") from None
    try:
        if not torch.cuda.is_available():
            raise BaselineError("CUDA")
        torch.cuda.set_device(0)
        if not torch.cuda.is_bf16_supported(including_emulation=False):
            raise BaselineError("BF16")
    except BaselineError:
        raise
    except Exception:
        raise BaselineError("CUDA") from None
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            settings["model_path"], local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(
            settings["model_path"], dtype=torch.bfloat16, device_map={"": 0},
            local_files_only=True, trust_remote_code=False, use_safetensors=True,
            attn_implementation="eager")
        model.eval()
    except Exception:
        raise BaselineError("LOAD") from None
    try:
        tensors = list(model.parameters()) + list(model.buffers())
        if not tensors or any(t.device.type != "cuda" or t.device.index != 0 for t in tensors):
            raise BaselineError("PLACEMENT")
        if any(t.is_floating_point() and t.dtype != torch.bfloat16 for t in model.parameters()):
            raise BaselineError("PLACEMENT")
    except BaselineError:
        raise
    except Exception:
        raise BaselineError("PLACEMENT") from None
    try:
        inputs = tokenizer.apply_chat_template(
            [{"role": "system", "content": settings["system_prompt"]},
             {"role": "user", "content": settings["prompt"]}],
            tokenize=True, add_generation_prompt=True, return_dict=True,
            return_tensors="pt")
        input_length = inputs["input_ids"].shape[1]
        if input_length + settings["max_new_tokens"] > model.config.max_position_embeddings:
            raise BaselineError("INPUT")
        inputs = inputs.to("cuda:0")
        generation = GenerationConfig(
            max_new_tokens=settings["max_new_tokens"], do_sample=False,
            eos_token_id=model.generation_config.eos_token_id,
            pad_token_id=tokenizer.eos_token_id)
    except BaselineError:
        raise
    except Exception:
        raise BaselineError("INPUT") from None
    try:
        with torch.inference_mode():
            outputs = model.generate(**inputs, generation_config=generation)
        return tokenizer.decode(outputs[0, input_length:], skip_special_tokens=True)
    except Exception:
        raise BaselineError("GENERATE") from None


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--model-path", default=os.environ.get("REPLICA_MODEL_PATH"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-new-tokens", type=int)
    try:
        args = parser.parse_args(argv)
        prompt = sys.stdin.read(16001)
        settings = read_settings(args.config, prompt, args.model_path, args.max_new_tokens)
        answer = infer(settings)
    except BaselineError as error:
        print(f"[{error.code}] {MESSAGES[error.code]}", file=sys.stderr)
        return 1
    except Exception:
        print(f"[GENERATE] {MESSAGES['GENERATE']}", file=sys.stderr)
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
