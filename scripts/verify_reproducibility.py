"""Optional real-data integration check; requires the companion database.

Run with UPbRPy installed (editable or released). Generated notebooks and
workbooks remain in the external output directory. Source files are linked,
not copied or modified. Exported code cells execute in a fresh namespace.
"""
import argparse
import json
import traceback
from pathlib import Path
import pandas as pd
from GUI.services import load_session, ensure_shared_pipeline
from GUI.workflows import get_workflow_preset
from UPbRPy.reproducibility import export_reduction_bundle
parser = argparse.ArgumentParser(description="Replay four real-data app exports and compare their numerical tables.")
parser.add_argument("--database", type=Path, required=True, help="LAICPMS_UPbR_database checkout")
parser.add_argument("--output", type=Path, required=True, help="External scratch output directory")
args = parser.parse_args()
ROOT = args.output.expanduser().absolute()
repository = Path(__file__).resolve().parents[1]
if ROOT == repository or repository in ROOT.parents:
    parser.error("Keep generated verification outputs outside the source checkout")
ROOT.mkdir(parents=True, exist_ok=True)
DATA = args.database.expanduser().resolve() / "data"
failures = []
for mineral,dataset in [('zircon','Zircon_28Mar25_1'),('apatite','Apatite_20Sep22'),('rutile','Rutile_28Feb24'),('titanite','Titanite_20Mar25')]:
 try:
  source=ROOT/dataset
  if not source.exists(): source.symlink_to(DATA/dataset,target_is_directory=True)
  preset=get_workflow_preset(mineral)
  session=load_session(str(source),preset=preset)
  pipeline=ensure_shared_pipeline(session,preset)
  print('LOADED',dataset,len(session.experiment),flush=True)
  bundle=export_reduction_bundle(pipeline,ROOT/(dataset+'_bundle'),hash_inputs=True)
  assert not bundle.warnings,bundle.warnings
  nb=json.loads((bundle.directory/'UPbRPy_reproducible_workflow.ipynb').read_text())
  ns={'__name__':'__main__'}
  for i,c in enumerate(nb['cells']):
   if c['cell_type']=='code':
    exec(compile(''.join(c['source']),f'{dataset}-export-cell-{i}','exec'),ns)
    ns['display']=lambda *a,**k:None
  for key,left,right in [('spot',pipeline.run_spot_means(),ns['spot_means']),('depth',pipeline.run_depth_profile(),ns['depth_profile']),('selected',pipeline.run_finalize().selected_results,ns['finalized'].selected_results)]:
   pd.testing.assert_frame_equal(left,right,check_exact=False,rtol=1e-10,atol=1e-10)
  print('PASS',dataset,'spots',len(ns['spot_means']),'sweeps',len(ns['depth_profile']),'files',len(bundle.files),flush=True)
 except Exception:
  failures.append(dataset)
  print('FAIL',dataset,flush=True);traceback.print_exc()

if failures:
    raise SystemExit("Failed datasets: " + ", ".join(failures))
