
const searchInput = document.querySelector(".search-box input");
const searchButton = document.querySelector(".search-box button");

const vehicles = document.querySelectorAll(".vehicle-card");

function searchVehicles() {

    let searchText = searchInput.value.toLowerCase();

    vehicles.forEach(function(vehicle) {

        let vehicleName = vehicle.innerText.toLowerCase();

        if (vehicleName.includes(searchText)) {

            vehicle.style.display = "block";

        } else {

            vehicle.style.display = "none";

        }

    });

}

searchButton.addEventListener("click", searchVehicles);
