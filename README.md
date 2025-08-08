# h2g-a Repository

## Setting Up the Environment

1. Navigate to the `h2g-a` directory.
2. Run the following command to create the environment:
    ```bash
    conda env create --file environment.yaml
    ```
3. Activate the environment using:
    ```bash
    conda activate pypsa-earth
    ```

To execute the entire workflow, use:
```bash
snakemake -j 1 all
```

## Functionality Overview and Instructions

This repository provides multiple functionalities organized into distinct work packages:

### Africa Hydrogen Analysis

To prepare the necessary data for the `africa_hydrogen_analysis.ipynb` notebook, run:
```bash
snakemake -j 1 retrieve_h2_networks
```
Afterward, open the notebook to explore the visualizations.

### Post-Processing Results

Run the following command:
```bash
snakemake -j 1 prepare_postprocessing
```
This will automatically execute the prerequisite rule `retrieve_result_networks`. Once completed, you will have all the required data to analyze the `plot_marginal_prices.ipynb` and `plot_summary.ipynb` notebooks.

## Handling Errors

The current framework for downloading network files is under development and may have connections/permissions issues. If you face them, just manually download the files using the link provided in the `Snakefile` and place them in the parent folder. The rest of the Snakemake workflow should work as expected.