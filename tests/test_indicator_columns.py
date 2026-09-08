from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.indicator_columns import indicator_output_columns
from nfi_backtest_engine.indicator_program import compile_indicator_program


@pytest.mark.parametrize('enabled', [False, True])
def test_optional_columns_follow_helper_frames_and_informative_suffixes(
    tmp_path: Path, enabled: bool,
) -> None:
    source = tmp_path / 'OptionalColumns.py'
    source.write_text(f'''
from freqtrade.strategy import IStrategy, merge_informative_pair
class OptionalColumns(IStrategy):
    timeframe = '5m'
    enabled = {enabled}
    def informative(self, metadata):
        frame = self.dp.get_pair_dataframe(pair=metadata['pair'], timeframe='1d')
        if self.enabled:
            frame['optional'] = frame['close'] * 2.0
        return frame
    def populate_indicators(self, dataframe, metadata):
        informative = self.informative(metadata)
        dataframe = merge_informative_pair(dataframe, informative, '5m', '1d', ffill=True)
        dataframe['base_only'] = dataframe['close']
        return dataframe
''')
    program = compile_indicator_program(source, class_name='OptionalColumns')
    columns = indicator_output_columns(program)
    assert ('optional_1d' in columns) is enabled
    assert 'optional' not in columns
    assert {'open', 'close', 'base_only', 'date_1d', 'close_1d'} <= columns
    # New frame opcodes cannot silently erase a callback's optional inputs.
    mutation = next(node for node in program['nodes'] if node['op'] == 'column-write')
    mutation['op'] = 'unreviewed-frame-operation'
    with pytest.raises(StrategyAnalysisError, match='column operation is unsupported'):
        indicator_output_columns(program)
