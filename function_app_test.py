import azure.functions as func
app = func.FunctionApp()

@app.schedule(schedule="0 0 0 * * *", arg_name="mytimer", run_on_startup=False, use_monitor=False)
def test_function(mytimer: func.TimerRequest) -> None:
    import logging
    logging.getLogger(__name__).info("test")
