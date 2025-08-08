rule retrieve_h2_networks:
    output:
        directory("networks/solved_h2")
    threads: 1
    shell:
        """
        gdown --folder https://drive.google.com/drive/folders/13YUrD8ZkR29UUivwwlqyLfmNWMmzdQsx
        mkdir -p networks/solved_h2
        cd "SolvedH2"
        mv * ../networks/solved_h2/
        cd ..
        rm -r "SolvedH2"
        """

rule retrieve_result_networks:
    output:
        results = directory("results/")
    threads: 1
    shell:
        """
        gdown --folder https://drive.google.com/drive/folders/1WeVS1ZiWYyndYQhuF_ORE7b57jhBqTbR
        """

rule prepare_postprocessing:
    input:
        results = directory("results/")
    threads: 1
    script:
        "scripts/make_stats_dicts.py"

rule all:
    input:
        "networks/solved_h2",
        "results/",