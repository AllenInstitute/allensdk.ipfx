CELL=$1
CELL_DIR=./test_data/specimen_$CELL
INPUT_JSON=$CELL_DIR/pipeline_input.json
OUTPUT_JSON=$CELL_DIR/pipeline_output.json

mkdir -p $CELL_DIR
python generate_pipeline_input.py $CELL
python run_pipeline.py --input_json $INPUT_JSON --output_json $OUTPUT_JSON --log_level DEBUG
python validate_experiment.py $INPUT_JSON $OUTPUT_JSON